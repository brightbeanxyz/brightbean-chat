"""Workspace-scoped views.

Every route here takes ``workspace_id``, which is ``RBACMiddleware``'s entire
resolution contract: by the time a view body runs, ``request.workspace`` and
``request.workspace_membership`` are set, and a workspace the user cannot reach
has already answered 404.
"""

from typing import Any

from django.apps import apps
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from apps.common.validators import is_valid_hex_color
from apps.members.decorators import require_permission
from apps.members.models import WorkspaceMembership
from apps.members.requests import WorkspaceRequest
from apps.workspaces.models import Workspace


@login_required
@require_GET
def dashboard(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """The workspace landing page. Every role can see it.

    The KPI cards are issue #26's, and they are fetched through
    :func:`apps.analytics.selectors.dashboard_kpis` — a late import, because
    ``apps.analytics`` sits far above this app and a deployment without it must
    still have a landing page. No cards is a degraded dashboard; an ImportError
    is no dashboard at all.
    """
    # select_related and the archived filter belong here, not in the template:
    # iterating memberships and touching .workspace costs one query each, on the
    # most-visited page in the app.
    switchable = (
        WorkspaceMembership.objects.filter(user=request.user, workspace__is_archived=False)
        .select_related("workspace")
        .order_by("workspace__name")
    )
    permissions = request.workspace_membership.effective_permissions
    return render(
        request,
        "workspaces/dashboard.html",
        {
            "switchable_memberships": switchable,
            "kpis": _kpis(request),
            # Each card links to the section that can explain its number, and a
            # link a viewer would be refused at is worse than no link — so the
            # cards that need a permission carry it rather than guessing from
            # the role.
            "can_view_analytics": permissions.get("view_analytics", False),
            "can_send_broadcasts": permissions.get("send_broadcasts", False),
        },
    )


def _kpis(request: WorkspaceRequest) -> dict[str, Any] | None:
    """The dashboard's numbers, or ``None`` when they should not be shown.

    ``None`` for two reasons: no analytics app, or a member without
    ``view_analytics``. Every workspace role holds that key today
    (``apps.members.roles``), so the second is not currently reachable — it is
    written anyway, because a permission that is only enforced where it happens
    to matter is a permission nobody can safely narrow later.
    """
    if not apps.is_installed("apps.analytics"):
        return None
    if not request.workspace_membership.effective_permissions.get("view_analytics", False):
        return None
    from apps.analytics.selectors import dashboard_kpis

    return dashboard_kpis(request.workspace)


@login_required
@require_POST
def switch(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """Make this the user's current workspace.

    POST rather than Studio's GET link: it writes ``last_workspace_id``, and a
    state-changing GET is both CSRF-exposed and prefetchable. The membership
    check has already happened in the middleware, so reaching here means the
    switch is legitimate — a role check would be wrong, since which workspace
    you are looking at is a personal preference, not workspace data.

    The write is done here rather than left to the middleware's own
    keep-in-sync side effect. That side effect is an optimisation for ordinary
    navigation; making the switcher depend on it would mean any future narrowing
    of it — skipping GETs, gating it behind a check — silently turns this view
    into a redirect that changes nothing.
    """
    request.user.last_workspace_id = request.workspace.pk
    request.user.save(update_fields=["last_workspace_id", "updated_at"])
    return redirect(reverse("workspaces:dashboard", kwargs={"workspace_id": workspace_id}))


@login_required
@require_permission("manage_workspace_settings")
@require_GET
def settings_view(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    return render(request, "workspaces/settings.html")


@login_required
@require_permission("manage_workspace_settings")
@require_POST
def update_settings(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    workspace = request.workspace
    # Strip first, then reject: `or workspace.name` only catches an empty
    # string, so "   " would survive it and be stored as a nameless workspace.
    name = (request.POST.get("name") or "").strip()[:100]
    if not name:
        messages.error(request, "A workspace needs a name.")
        return redirect(reverse("workspaces:settings", kwargs={"workspace_id": workspace_id}))
    # (organization, name) is unique, so without this an ordinary rename onto a
    # sibling's name reaches the constraint and 500s. The create flow already
    # checks; this one has to as well.
    clash = Workspace.objects.for_org(workspace.organization_id).filter(name=name).exclude(pk=workspace.pk)
    if clash.exists():
        messages.error(request, "Another workspace in this organization already has that name.")
        return redirect(reverse("workspaces:settings", kwargs={"workspace_id": workspace_id}))
    workspace.name = name
    workspace.icon = (request.POST.get("icon") or "").strip()[:8]
    workspace.description = (request.POST.get("description") or "").strip()[:500]
    workspace.timezone = (request.POST.get("timezone") or "").strip()[:63]

    for field in ("primary_color", "secondary_color"):
        value = (request.POST.get(field) or "").strip()
        if not is_valid_hex_color(value):
            messages.error(request, "Colours must be a 6-digit hex value like #3B82F6.")
            return redirect(reverse("workspaces:settings", kwargs={"workspace_id": workspace_id}))
        setattr(workspace, field, value)

    workspace.save(
        update_fields=["name", "icon", "description", "timezone", "primary_color", "secondary_color", "updated_at"]
    )
    messages.success(request, "Workspace settings saved.")
    return redirect(reverse("workspaces:settings", kwargs={"workspace_id": workspace_id}))
