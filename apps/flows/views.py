"""The flow list and the builder's host page.

Reads are open to any workspace member and writes require ``edit_flows`` — the
same split as the data API, and for the same reason (see
:mod:`apps.flows.api`). A Viewer can open the list and the builder; the builder
is handed ``can_edit`` so L3-C can render read-only rather than letting someone
drag nodes around and discover on save that they may not.

Everything except the builder page is HTMX: the mutations answer with a toast
and a ``flowsChanged`` event, and the list re-fetches its own rows. That keeps
one renderer for the table instead of one for the page and one for each action.
"""

from typing import Any

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from apps.common.htmx import toast_response
from apps.common.shortcuts import get_scoped_object_or_404
from apps.flows import services
from apps.flows.models import Flow, FlowStatus
from apps.members.decorators import require_permission, require_workspace_role
from apps.members.requests import WorkspaceRequest
from apps.members.roles import WorkspaceRole

__all__ = [
    "flow_archive",
    "flow_create",
    "flow_duplicate",
    "flow_edit",
    "flow_list",
    "flow_rename",
    "flow_restore",
]

# Viewer is the floor of the role ladder, so this is "any member". Same gate as
# the API's read endpoints.
require_workspace_member = require_workspace_role(WorkspaceRole.VIEWER)

#: What the "no folder" group is called on screen.
UNFILED_LABEL = "Unfiled"

#: The query-string value that selects it. Deliberately *not* the label: folder
#: names are free user text, so filtering on "Unfiled" made a folder actually
#: called Unfiled unreachable — picking it showed the unfiled flows instead. A
#: dunder token sits outside the namespace anyone types into a folder field.
UNFILED_VALUE = "__unfiled__"

_MAX_NAME = Flow._meta.get_field("name").max_length or 200


def _visible_flows(request: WorkspaceRequest) -> Any:
    """The workspace's flows, filtered by the toolbar."""
    flows = Flow.objects.for_workspace(request.workspace)

    query = (request.GET.get("q") or "").strip()
    if query:
        flows = flows.filter(name__icontains=query)

    # Anything unrecognised falls back to the default view rather than to no
    # filtering at all: an if/elif here let `?status=bogus` match neither branch
    # and so skip the exclusion, quietly listing archived flows among the live
    # ones. Archived flows are out of the way by default but still findable —
    # "Archived" in the status filter is the only way to see them, which is what
    # archiving is for.
    status = (request.GET.get("status") or "").strip()
    flows = flows.filter(status=status) if status in FlowStatus.values else flows.exclude(status=FlowStatus.ARCHIVED)

    folder = (request.GET.get("folder") or "").strip()
    if folder == UNFILED_VALUE:
        flows = flows.filter(folder="")
    elif folder:
        flows = flows.filter(folder=folder)

    return flows.order_by("folder", "name")


def _list_context(request: WorkspaceRequest) -> dict[str, Any]:
    flows = list(_visible_flows(request))

    # Runs are detected on the folder value, not on the label it renders under:
    # a workspace holding both unfiled flows and a folder literally named
    # "Unfiled" produces two identical labels, and comparing those merged two
    # genuinely different groups into one.
    groups: list[dict[str, Any]] = []
    for flow in flows:
        if not groups or groups[-1]["key"] != flow.folder:
            groups.append({"key": flow.folder, "label": flow.folder or UNFILED_LABEL, "flows": []})
        groups[-1]["flows"].append(flow)

    # The folder filter offers every folder in the workspace, not just the ones
    # surviving the current filter — otherwise picking one erases the rest of
    # the menu and there is no way back.
    folders = (
        Flow.objects.for_workspace(request.workspace)
        .exclude(folder="")
        .order_by("folder")
        .values_list("folder", flat=True)
        .distinct()
    )

    folder_names = list(folders)
    return {
        "groups": groups,
        "flow_count": len(flows),
        # (value, label) pairs, which is what ui_select wants — and what keeps
        # the "Unfiled" row's value distinct from a folder of the same name.
        "folder_options": [(UNFILED_VALUE, UNFILED_LABEL), *((name, name) for name in folder_names)],
        "status_options": list(FlowStatus.choices),
        "query": request.GET.get("q", ""),
        "status": request.GET.get("status", ""),
        "folder": request.GET.get("folder", ""),
        "can_edit": request.workspace_membership.effective_permissions.get("edit_flows", False),
        "unfiled_label": UNFILED_LABEL,
    }


@login_required
@require_workspace_member
@require_GET
def flow_list(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """The flow list. Answers the rows partial to HTMX and the page otherwise."""
    context = _list_context(request)
    template = "flows/_list_rows.html" if request.headers.get("HX-Request") else "flows/list.html"
    return render(request, template, context)


@login_required
@require_workspace_member
@ensure_csrf_cookie
@require_GET
def flow_edit(request: WorkspaceRequest, workspace_id: str, flow_id: str) -> HttpResponse:
    """The builder's host page: a mount div and a placeholder.

    L3-C (issue #10) mounts the React island here. The URLs it needs are
    ``data-`` attributes rather than something it reverses itself, and
    ``ensure_csrf_cookie`` guarantees the token is there for the first PUT —
    without it an autosave two seconds after load would be the request that
    finds no cookie.
    """
    flow = get_scoped_object_or_404(Flow, request.workspace, pk=flow_id)
    version = services.latest_version(flow)
    keys = {"workspace_id": workspace_id, "flow_id": flow.pk}
    return render(
        request,
        "flows/edit.html",
        {
            "flow": flow,
            "version": version,
            "can_edit": request.workspace_membership.effective_permissions.get("edit_flows", False),
            "api_detail_url": reverse("flows:api_detail", kwargs=keys),
            "api_publish_url": reverse("flows:api_publish", kwargs=keys),
            "api_stats_url": reverse("flows:api_stats", kwargs=keys),
            "api_schema_url": reverse("flows:api_schema", kwargs={"workspace_id": workspace_id}),
            # #16's picker, for the send_message media block. Reversed here like
            # its four siblings rather than assembled from location.pathname in
            # the bundle, which would break under FORCE_SCRIPT_NAME.
            "media_picker_url": reverse("media:picker", kwargs={"workspace_id": workspace_id}),
            "list_url": reverse("flows:list", kwargs={"workspace_id": workspace_id}),
        },
    )


def _name_from(request: WorkspaceRequest, fallback: str = "") -> str:
    return (request.POST.get("name") or fallback).strip()[:_MAX_NAME]


@login_required
@require_permission("edit_flows")
@require_POST
def flow_create(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    name = _name_from(request)
    if not name:
        return toast_response(tone="error", title="Name required", body="Give the flow a name to create it.")
    folder = (request.POST.get("folder") or "").strip()[:_MAX_NAME]
    flow = services.create_flow(workspace=request.workspace, name=name, folder=folder, user=request.user)
    return toast_response(
        tone="success",
        title="Flow created",
        body=f"{flow.name} is ready to edit.",
        events={"flowsChanged": True},
    )


@login_required
@require_permission("edit_flows")
@require_POST
def flow_rename(request: WorkspaceRequest, workspace_id: str, flow_id: str) -> HttpResponse:
    flow = get_scoped_object_or_404(Flow, request.workspace, pk=flow_id)
    name = _name_from(request)
    if not name:
        return toast_response(tone="error", title="Name required", body="A flow needs a name.")
    services.rename_flow(flow, name)
    if "folder" in request.POST:
        services.set_folder(flow, (request.POST.get("folder") or "").strip()[:_MAX_NAME])
    return toast_response(tone="success", title="Flow renamed", events={"flowsChanged": True})


@login_required
@require_permission("edit_flows")
@require_POST
def flow_duplicate(request: WorkspaceRequest, workspace_id: str, flow_id: str) -> HttpResponse:
    flow = get_scoped_object_or_404(Flow, request.workspace, pk=flow_id)
    copy = services.duplicate_flow(flow, user=request.user)
    return toast_response(
        tone="success",
        title="Flow duplicated",
        body=f"{copy.name} was created as a draft.",
        events={"flowsChanged": True},
    )


@login_required
@require_permission("edit_flows")
@require_POST
def flow_archive(request: WorkspaceRequest, workspace_id: str, flow_id: str) -> HttpResponse:
    flow = get_scoped_object_or_404(Flow, request.workspace, pk=flow_id)
    services.archive_flow(flow)
    return toast_response(
        tone="info",
        title="Flow archived",
        body="Find it again with the Archived status filter.",
        events={"flowsChanged": True},
    )


@login_required
@require_permission("edit_flows")
@require_POST
def flow_restore(request: WorkspaceRequest, workspace_id: str, flow_id: str) -> HttpResponse:
    flow = get_scoped_object_or_404(Flow, request.workspace, pk=flow_id)
    services.restore_flow(flow)
    return toast_response(tone="success", title="Flow restored", events={"flowsChanged": True})
