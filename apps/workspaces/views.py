"""Workspace-scoped views.

Every route here takes ``workspace_id``, which is ``RBACMiddleware``'s entire
resolution contract: by the time a view body runs, ``request.workspace`` and
``request.workspace_membership`` are set, and a workspace the user cannot reach
has already answered 404.
"""

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


@login_required
@require_GET
def dashboard(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """The workspace landing page. Every role can see it."""
    # select_related and the archived filter belong here, not in the template:
    # iterating memberships and touching .workspace costs one query each, on the
    # most-visited page in the app.
    switchable = (
        WorkspaceMembership.objects.filter(user=request.user, workspace__is_archived=False)
        .select_related("workspace")
        .order_by("workspace__name")
    )
    return render(request, "workspaces/dashboard.html", {"switchable_memberships": switchable})


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
