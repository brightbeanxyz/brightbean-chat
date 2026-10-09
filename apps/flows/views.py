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

from datetime import timedelta
from typing import Any

from django.apps import apps as django_apps
from django.contrib.auth.decorators import login_required
from django.db.models import Count, OuterRef, Q, Subquery
from django.http import HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from apps.common.filters import multi
from apps.common.htmx import toast_response
from apps.common.platforms import Platform
from apps.common.shortcuts import get_scoped_object_or_404
from apps.flows import services
from apps.flows.capabilities import connected_platforms
from apps.flows.models import Flow, FlowExecution, FlowStatus, FlowVersion
from apps.flows.portability.cards import card_contexts
from apps.flows.portability.library import STARTER_CATEGORY
from apps.flows.portability.library import template_cards as shipped_templates
from apps.flows.starter import starter_graph
from apps.flows.triggers.phrasing import describe_triggers
from apps.flows.triggers.platforms import shown_platforms
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

#: Stands in for a trigger id in the builder's trigger-switch URL template; the
#: island swaps in the real one (src/env.ts). The nil UUID, which no row has.
TRIGGER_ID_PLACEHOLDER = "00000000-0000-0000-0000-000000000000"

#: How many template cards the flows empty state shows before deferring to the
#: full gallery. Enough to suggest the range, few enough that the Create field
#: above them is still the obvious alternative.
EMPTY_STATE_TEMPLATES = 4


def _featured(cards: list[Any], limit: int) -> list[Any]:
    """The first ``limit`` cards to show somebody who has nothing yet.

    Starters first. ``template_paths()`` is alphabetical, which puts four
    near-identical ``instagram-comment-*`` cards at the front — the same channel
    four times over, from the one screen meant to suggest the range.
    """
    return sorted(cards, key=lambda card: card.category != STARTER_CATEGORY)[:limit]


def _visible_flows(request: WorkspaceRequest) -> Any:
    """The workspace's flows, filtered by the toolbar."""
    flows = Flow.objects.for_workspace(request.workspace)

    query = (request.GET.get("q") or "").strip()
    if query:
        flows = flows.filter(name__icontains=query)

    # Both filters repeat (`?status=active&status=offline`): the Filter popover
    # lets a reader pick several values in a group. multi() drops anything
    # unrecognised, so `?status=bogus` reads as no status filter — and no status
    # filter means the default view rather than no filtering at all: an
    # unrecognised value once skipped the exclusion and quietly listed archived
    # flows among the live ones. Archived flows are out of the way by default
    # but still findable — "Archived" in the status filter is the only way to
    # see them, which is what archiving is for.
    statuses = multi(request.GET, "status", allowed=FlowStatus.values)
    flows = flows.filter(status__in=statuses) if statuses else flows.exclude(status=FlowStatus.ARCHIVED)

    folders = multi(request.GET, "folder")
    if folders:
        named = Q(folder__in=[folder for folder in folders if folder != UNFILED_VALUE])
        flows = flows.filter(named | Q(folder="") if UNFILED_VALUE in folders else named)

    return flows.order_by("name")


#: The list's sections, in the order a reader scans them (HANDOFF §3, Flows):
#: what is running, what was running and was switched off, what was never
#: finished. Archived only appears when the status filter asks for it. The tone
#: is the status-pill tone the section's dot and rows wear.
SECTIONS: tuple[tuple[str, str, str], ...] = (
    (FlowStatus.ACTIVE, "Live", "success"),
    (FlowStatus.OFFLINE, "Offline", "warning"),
    (FlowStatus.DRAFT, "Draft", "neutral"),
    (FlowStatus.ARCHIVED, "Archived", "neutral"),
)

#: The window the row's run count covers.
RUNS_WINDOW = timedelta(days=7)


def _list_context(request: WorkspaceRequest) -> dict[str, Any]:
    # prefetch_related here rather than a second pass: the summaries below read
    # every flow's triggers, and re-fetching the same rows by pk to prefetch
    # them cost an extra query plus a dict that existed only to join the answer
    # back onto objects already in hand.
    # `latest_published_at` answers "has the newest version ever been live?",
    # which is what decides whether an offline flow's switch may turn it back
    # on (see flow_set_live): a newer, never-live version means edits nobody
    # has reviewed. One correlated subquery for the page, not one per row.
    latest_version = (
        FlowVersion.objects.for_workspace(request.workspace)
        .filter(flow=OuterRef("pk"))
        .order_by("-version")
        .values("published_at")[:1]
    )
    flows = list(
        _visible_flows(request)
        .annotate(latest_published_at=Subquery(latest_version))
        .prefetch_related("triggers__channel_connection")
    )

    # The redesign's filter chips carry counts, so a reader can see there are
    # two drafts without selecting the filter to find out. One grouped query
    # rather than four counts, and "all" deliberately excludes archived — the
    # chip means "everything you would normally be looking at", which is what
    # the unfiltered list shows.
    by_status = dict(
        Flow.objects.for_workspace(request.workspace)
        .values_list("status")
        .annotate(total=Count("id"))
        .values_list("status", "total")
    )
    status_counts = {
        "": sum(total for status, total in by_status.items() if status != FlowStatus.ARCHIVED),
        **{str(status): by_status.get(status, 0) for status in FlowStatus.values},
    }

    # How many conversations each flow started in the last week: the row's one
    # figure. Builder previews are not runs anybody had. One grouped query.
    runs = dict(
        FlowExecution.objects.for_workspace(request.workspace)
        .filter(flow__in=[flow.pk for flow in flows], preview=False, created_at__gte=timezone.now() - RUNS_WINDOW)
        .values_list("flow_id")
        .annotate(total=Count("id"))
        .values_list("flow_id", "total")
    )

    # One sentence per flow saying when it runs, in the reader's words rather
    # than SPEC §10's. Reads the prefetch above, so this is no queries at all.
    # The channel glyphs on each row read the same prefetch; the connected set
    # is one query for the page, for the unbound triggers.
    connected = set(connected_platforms(request.workspace))
    for flow in flows:
        flow.trigger_summary = describe_triggers(list(flow.triggers.all()))
        flow.platforms = [
            {"key": key, "label": Platform(key).label}
            for key in shown_platforms(flow.triggers.all(), connected=connected)
        ]
        flow.runs_recent = runs.get(flow.pk, 0)
        # The switch can always turn a live flow off; it can turn an offline one
        # back on only when nothing has changed since it last ran.
        flow.can_switch = flow.status == FlowStatus.ACTIVE or (
            flow.status == FlowStatus.OFFLINE and flow.latest_published_at is not None
        )

    # Sections by status (HANDOFF §3). Folders are a filter and a word on the
    # row rather than the grouping: the question a reader brings to this page is
    # "what is running", and a folder heading answered "where did I file it".
    groups: list[dict[str, Any]] = [
        {"key": status, "label": label, "tone": tone, "flows": [f for f in flows if f.status == status]}
        for status, label, tone in SECTIONS
    ]
    groups = [group for group in groups if group["flows"]]

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
    can_edit = request.workspace_membership.effective_permissions.get("edit_flows", False)
    statuses = multi(request.GET, "status", allowed=FlowStatus.values)
    folder_values = multi(request.GET, "folder")
    filtered = bool((request.GET.get("q") or "").strip() or statuses or folder_values)

    # Templates in the empty state, and only there: this is the exact moment
    # somebody has nothing and no idea what to build, and the page offered them
    # a naked text field.
    #
    # `has_no_flows`, not `not groups`. An empty *view* is not an empty
    # workspace: _visible_flows excludes archived flows unless the status filter
    # asks for them, so somebody who archived all twenty of theirs would be
    # shown a first-run gallery and told they have no flows. The extra query
    # only runs once the cheap checks have passed, and stops the moment the
    # first Create lands, since the HTMX refresh re-renders with groups.
    #
    # shipped_templates() digests every file on disk even on a cache hit, so it
    # stays inside the guard — do not hoist it. `by_status` above already counts
    # every flow in the workspace, archived included, so the emptiness question
    # costs no query of its own.
    template_cards: list[dict[str, Any]] = []
    template_total = 0
    if not groups and not filtered and can_edit:
        has_no_flows = sum(by_status.values()) == 0
        if has_no_flows:
            cards = shipped_templates()
            template_total = len(cards)
            template_cards = card_contexts(request.workspace, _featured(cards, EMPTY_STATE_TEMPLATES))

    return {
        "groups": groups,
        "flow_count": len(flows),
        # The three cards over the list. Live / Offline / Draft only: archived
        # is the state nobody needs to act on, and it has its filter.
        "summary": [
            {"status": status, "label": label, "tone": tone, "count": status_counts.get(str(status), 0)}
            for status, label, tone in SECTIONS[:3]
        ],
        "template_cards": template_cards,
        "template_total": template_total,
        "query": request.GET.get("q", ""),
        # The Filter popover's state and its sections. `filters` seeds the
        # page's Alpine model, so a reload or a shared link opens with the same
        # selection it was made with.
        "filters": {"status": statuses, "folder": folder_values},
        "filter_groups": [
            # Each status carries its count, so nobody has to pick a filter to
            # find out it is empty. Labels rather than the enum's: "Live" says
            # what an active flow is doing, "Active" says what a column holds.
            {
                "key": "status",
                "label": "Status",
                "options": [
                    {"value": value, "label": label, "count": status_counts.get(str(value), 0)}
                    for value, label in (
                        (FlowStatus.ACTIVE, "Live"),
                        (FlowStatus.OFFLINE, "Offline"),
                        (FlowStatus.DRAFT, "Draft"),
                        (FlowStatus.ARCHIVED, "Archived"),
                    )
                ],
            },
            # (value, label) pairs, which keep the "Unfiled" option's value
            # distinct from a folder of the same name. Every folder in the
            # workspace, not just the ones surviving the current filter —
            # otherwise picking one erases the rest and there is no way back.
            {
                "key": "folder",
                "label": "Folder",
                "options": [(UNFILED_VALUE, UNFILED_LABEL), *((name, name) for name in folder_names)],
            },
        ],
        "can_edit": can_edit,
        # Issue #26's per-flow stats page. Gated on its own key rather than on
        # edit_flows: reading numbers and changing a graph are different rights,
        # and every role holds this one today.
        #
        # ANDed with the app being installed, because the template reverses
        # `analytics:flow_detail` behind this flag and config/urls.py only mounts
        # that route when apps.analytics is there — a permission check alone
        # would be a NoReverseMatch on the flow list of a deployment that drops
        # the app. "May this person see analytics" is false when there are none.
        "can_view_analytics": (
            request.workspace_membership.effective_permissions.get("view_analytics", False)
            and django_apps.is_installed("apps.analytics")
        ),
        "unfiled_label": UNFILED_LABEL,
    }


@login_required
@require_workspace_member
@require_GET
def flow_list(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """The flow list. Answers the rows partial to HTMX and the page otherwise."""
    context = _list_context(request)
    if request.headers.get("HX-Request"):
        # The summary cards sit above the toolbar, outside the swapped region;
        # the rows response refreshes them out of band (flows/_summary.html).
        return render(request, "flows/_list_rows.html", {**context, "summary_oob": True})
    return render(request, "flows/list.html", context)


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
    # No version is fetched for the page. The header cannot re-render, so
    # anything it said about draft-versus-live went stale the moment the user
    # published; the island reads both from the flow API instead.
    keys = {"workspace_id": workspace_id, "flow_id": flow.pk}
    return render(
        request,
        "flows/edit.html",
        {
            "flow": flow,
            "can_edit": request.workspace_membership.effective_permissions.get("edit_flows", False),
            "api_detail_url": reverse("flows:api_detail", kwargs=keys),
            "api_publish_url": reverse("flows:api_publish", kwargs=keys),
            "api_offline_url": reverse("flows:api_offline", kwargs=keys),
            "api_stats_url": reverse("flows:api_stats", kwargs=keys),
            "api_schema_url": reverse("flows:api_schema", kwargs={"workspace_id": workspace_id}),
            # #27's export, offered from the builder as well as from the list.
            # Reversed here like the URLs above rather than assembled in the
            # bundle, which would break under FORCE_SCRIPT_NAME.
            "export_url": reverse("flows:export", kwargs=keys),
            "export_bundle_url": reverse("flows:export_bundle", kwargs=keys),
            # #16's picker, for the send_message media block. Reversed here like
            # its four siblings rather than assembled from location.pathname in
            # the bundle, which would break under FORCE_SCRIPT_NAME.
            "media_picker_url": reverse("media:picker", kwargs={"workspace_id": workspace_id}),
            # SPEC §16's preview, no longer Telegram-only. The endpoint lives
            # in the channels app — it reads a connection and mints a
            # channel-specific deep link — and is reversed here for the same
            # reason the picker is: the island assembles no URLs of its own.
            "preview_url": reverse("channels:flow_preview", kwargs=keys),
            "list_url": reverse("flows:list", kwargs={"workspace_id": workspace_id}),
            "api_rename_url": reverse("flows:api_rename", kwargs=keys),
            # A template, not a URL: the island substitutes a trigger's id for
            # the zero UUID. Reversed here so it still assembles no path of its
            # own (FORCE_SCRIPT_NAME), and the zero UUID can never be a real id.
            "api_trigger_enabled_url": reverse(
                "flows:api_trigger_enabled", kwargs={**keys, "trigger_id": TRIGGER_ID_PLACEHOLDER}
            ),
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
    # The one caller that asks for a starter graph. Everything else that creates
    # a flow — the importer, the broadcast composer — writes its own version 1
    # immediately afterwards. See apps.flows.starter.
    flow = services.create_flow(
        workspace=request.workspace,
        name=name,
        folder=folder,
        user=request.user,
        graph=starter_graph(),
    )
    return toast_response(
        tone="success",
        title="Flow created",
        body=f"{flow.name} starts with a first message. Open it to edit.",
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
def flow_set_live(request: WorkspaceRequest, workspace_id: str, flow_id: str) -> HttpResponse:
    """The flow list's switch: ``live=0`` sets a live flow offline, ``live=1``
    sets an offline one live again.

    The target state is explicit rather than a toggle, so a page that has gone
    stale — another tab already flipped it — cannot flip it back by accident;
    it gets a refusal and the row re-reads itself.

    **Back on only re-runs what already ran.** :func:`services.publish` publishes
    the *newest* version, and a flow edited since it went offline has a newer
    one nobody has set live yet. A switch on a list row is no place to publish
    unreviewed edits, so that case is refused here with a pointer to the
    builder, where the changes are in front of the person setting them live.
    The list draws that switch disabled for the same reason; this is the check
    that holds when the page is stale.
    """
    flow = get_scoped_object_or_404(Flow, request.workspace, pk=flow_id)
    if request.POST.get("live") == "0":
        try:
            stopped = services.take_offline(flow)
        except services.FlowNotLiveError as exc:
            return toast_response(tone="error", title="Not set offline", body=str(exc), events={"flowsChanged": True})
        body = "It stopped replying straight away."
        if stopped:
            body = f"It stopped replying straight away, including to {stopped} {'person' if stopped == 1 else 'people'} partway through it."
        return toast_response(tone="success", title="Flow is offline", body=body, events={"flowsChanged": True})

    latest = services.latest_version(flow)
    if flow.status != FlowStatus.OFFLINE or latest is None or latest.published_at is None:
        return toast_response(
            tone="error",
            title="Not set live",
            body="It has changes that have not been live yet. Open it to review them, then set it live there.",
            events={"flowsChanged": True},
        )
    try:
        services.publish(flow, user=request.user)
    except services.FlowValidationError:
        return toast_response(
            tone="error",
            title="Not set live",
            body="It has problems to fix first. Open it to see them.",
            events={"flowsChanged": True},
        )
    except services.FlowPlanLimitError as exc:
        return toast_response(tone="error", title="Not set live", body=str(exc), events={"flowsChanged": True})
    return toast_response(tone="success", title="Flow is live", events={"flowsChanged": True})


@login_required
@require_permission("edit_flows")
@require_POST
def flow_restore(request: WorkspaceRequest, workspace_id: str, flow_id: str) -> HttpResponse:
    flow = get_scoped_object_or_404(Flow, request.workspace, pk=flow_id)
    try:
        services.restore_flow(flow)
    except services.FlowPlanLimitError as exc:
        # Restoring a flow with a published version puts it back live, which
        # takes the same plan check as publishing — and uncaught it was a 500.
        return toast_response(tone="error", title="Not restored", body=str(exc))
    return toast_response(tone="success", title="Flow restored", events={"flowsChanged": True})
