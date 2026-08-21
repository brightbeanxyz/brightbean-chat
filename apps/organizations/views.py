"""Organization-level settings.

Org routes carry no organization id in the URL: v1 is one organization per user
and ``RBACMiddleware`` resolves it (see that module's docstring on the
assumption and what changes if multi-org ever arrives).
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from apps.members.decorators import require_org_role
from apps.members.models import WorkspaceMembership
from apps.members.requests import OrgRequest
from apps.members.roles import OrgRole, WorkspaceRole
from apps.workspaces.models import Workspace


def _can_manage(request: OrgRequest) -> bool:
    return request.org_membership.org_role in (OrgRole.OWNER, OrgRole.ADMIN)


@login_required
@require_org_role("member")
@require_GET
def settings_view(request: OrgRequest) -> HttpResponse:
    return render(request, "organizations/settings.html", {"can_manage": _can_manage(request)})


@login_required
@require_org_role("admin")
@require_POST
def update_settings(request: OrgRequest) -> HttpResponse:
    org = request.org
    # Strip first, then reject — see apps/workspaces/views.py for the same trap.
    name = (request.POST.get("name") or "").strip()[:100]
    if not name:
        messages.error(request, "An organization needs a name.")
        return redirect(reverse("organizations:settings"))
    org.name = name
    org.default_timezone = (request.POST.get("default_timezone") or org.default_timezone).strip()[:63]
    org.logo_url = (request.POST.get("logo_url") or "").strip()
    org.save(update_fields=["name", "default_timezone", "logo_url", "updated_at"])
    messages.success(request, "Organization settings saved.")
    return redirect(reverse("organizations:settings"))


@login_required
@require_org_role("member")
@require_GET
def workspaces_view(request: OrgRequest) -> HttpResponse:
    """Every workspace in the org, archived ones included.

    This is the only place an archived workspace is visible: ``/w/<id>/`` 404s
    for them (``RBACMiddleware``), so without this list there is no way back.
    """
    workspaces = Workspace.objects.for_org(request.org.pk).order_by("is_archived", "name")
    member_workspace_ids = set(
        WorkspaceMembership.objects.filter(user=request.user, workspace__organization=request.org).values_list(
            "workspace_id", flat=True
        )
    )
    return render(
        request,
        "organizations/workspaces.html",
        {
            "workspaces": workspaces,
            "member_workspace_ids": member_workspace_ids,
            "can_manage": _can_manage(request),
        },
    )


@login_required
@require_org_role("admin")
@require_POST
def create_workspace(request: OrgRequest) -> HttpResponse:
    name = (request.POST.get("name") or "").strip()[:100]
    if not name:
        messages.error(request, "A workspace needs a name.")
        return redirect(reverse("organizations:workspaces"))
    if Workspace.objects.for_org(request.org.pk).filter(name=name).exists():
        messages.error(request, "A workspace with that name already exists.")
        return redirect(reverse("organizations:workspaces"))

    workspace = Workspace.objects.create(organization=request.org, name=name)
    # The creator becomes its admin, or nobody can configure the thing they
    # just made.
    WorkspaceMembership.objects.create(user=request.user, workspace=workspace, workspace_role=WorkspaceRole.ADMIN)
    messages.success(request, f"Created {workspace.name}.")
    return redirect(reverse("organizations:workspaces"))


@login_required
@require_org_role("admin")
@require_POST
def set_workspace_archived(request: OrgRequest, target_id: str) -> HttpResponse:
    """Archive or restore a workspace.

    The kwarg is ``target_id``, not ``workspace_id``, and that is load-bearing:
    ``workspace_id`` is ``RBACMiddleware``'s resolution contract, and the
    middleware 404s archived workspaces — so naming it that would make
    unarchiving impossible. Tenancy is enforced here instead, by scoping the
    lookup to ``request.org`` and answering 404 on a miss.
    """
    workspace = Workspace.objects.for_org(request.org.pk).filter(pk=target_id).first()
    if workspace is None:
        raise Http404("No such workspace.")

    workspace.is_archived = request.POST.get("archived") == "1"
    workspace.save(update_fields=["is_archived", "updated_at"])
    messages.success(request, f"{'Archived' if workspace.is_archived else 'Restored'} {workspace.name}.")
    return redirect(reverse("organizations:workspaces"))
