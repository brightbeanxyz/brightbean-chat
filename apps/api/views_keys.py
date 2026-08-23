"""Org-level API key management, mounted at ``/organization/api-keys/``.

SPEC §4.1 puts ``manage_api_keys`` at the organization tier — a key spans "any
workspace in the org" — and ``config/urls.py`` has carried a stub at this exact
path, named for this issue, since Layer 1. This replaces the stub; the URL name
``settings_org_api_keys`` and the nav row that reverses it are unchanged, which
is the contract that file's comment states.

Authority is checked twice, at two tiers, and the second check is the one people
forget: ``@require_org_role("admin")`` gets you the page, and
``apps.api.services.issue_api_key`` separately requires ``manage_api_keys`` in
the *target* workspace. An org admin who is not an admin of one workspace does
not get to mint a credential inside it.

**The plaintext key is rendered in the response to the POST that created it.**
No redirect, no ``messages`` framework — messages are stored in the session,
which in this project is a database table, so a redirect-then-show would leave a
live credential sitting in ``django_session`` for the life of the session
(SECURITY-BASELINE §5). ``apps/channels/views.py`` gives up Post/Redirect/Get
for the same reason and says so.
"""

from __future__ import annotations

from typing import Any

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from apps.api.models import ApiKey
from apps.api.services import ApiKeysError, issue_api_key, known_scopes, revoke_api_key, scope_summary
from apps.members.decorators import require_org_role
from apps.members.models import WorkspaceMembership
from apps.members.requests import OrgRequest
from apps.workspaces.models import Workspace

__all__ = ["issue_key", "key_list", "revoke_key"]


def _issuable_workspaces(request: OrgRequest) -> list[Workspace]:
    """The workspaces this user may actually mint a key for.

    Filtered by the *workspace* gate rather than listed from the org, so the
    dropdown offers only what ``issue_api_key`` would accept. A picker that
    offers a choice the server then refuses is a picker that teaches people the
    product is broken.
    """
    memberships = WorkspaceMembership.objects.filter(
        user=request.user, workspace__organization=request.org, workspace__is_archived=False
    ).select_related("workspace")
    return [
        membership.workspace
        for membership in memberships
        if membership.effective_permissions.get("manage_api_keys", False)
    ]


def _rows(request: OrgRequest) -> list[dict[str, Any]]:
    """Every key in the org, newest first, with its workspace and scopes.

    ``.unscoped()`` because this page is org-tier by design: it spans every
    workspace in the organization, which is the capability SPEC §4.1 describes.
    The filter on ``workspace__organization`` is what bounds it, and it is the
    same bound ``RBACMiddleware`` gives every other page under
    ``/organization/``.
    """
    keys = (
        # Cross-workspace on purpose: an org-tier page lists the org's keys.
        ApiKey.objects.unscoped()
        .filter(workspace__organization=request.org)
        .select_related("workspace")
        .order_by("-created_at")
    )
    return [
        {
            "key": key,
            "workspace": key.workspace,
            "scopes": ", ".join(key.scopes or []),
            "grants": scope_summary(key),
        }
        for key in keys
    ]


def _context(request: OrgRequest, **extra: Any) -> dict[str, Any]:
    return {
        "rows": _rows(request),
        "workspaces": _issuable_workspaces(request),
        "scope_choices": known_scopes(),
        **extra,
    }


@login_required
@require_org_role("admin")
@require_GET
def key_list(request: OrgRequest) -> HttpResponse:
    """List every key in the organization."""
    return render(request, "api/keys_list.html", _context(request))


@login_required
@require_org_role("admin")
@require_POST
def issue_key(request: OrgRequest) -> HttpResponse:
    """Mint a key and render it exactly once.

    A refusal re-renders the list with the error rather than redirecting, so the
    operator keeps what they typed.
    """
    workspace_id = request.POST.get("workspace") or ""
    workspace = (
        Workspace.objects.for_org(request.org.pk).filter(pk=workspace_id, is_archived=False).first()
        if workspace_id
        else None
    )
    if workspace is None:
        # Deliberately the same message for "not a workspace id" and "not a
        # workspace in your organization": the second would confirm the id names
        # something real somewhere else (SECURITY-BASELINE §1).
        return render(request, "api/keys_list.html", _context(request, error="Choose a workspace."), status=400)

    try:
        api_key = issue_api_key(
            workspace=workspace,
            issuer=request.user,
            name=request.POST.get("name", ""),
            scopes=request.POST.getlist("scopes"),
        )
    except ApiKeysError as exc:
        return render(request, "api/keys_list.html", _context(request, error=str(exc)), status=400)

    return render(
        request,
        "api/key_created.html",
        {
            "api_key": api_key,
            "workspace": workspace,
            "plaintext": api_key.raw_token,
            "back_url": reverse("settings_org_api_keys"),
        },
    )


@login_required
@require_org_role("admin")
@require_POST
def revoke_key(request: OrgRequest, api_key_id: Any) -> HttpResponse:
    """Revoke a key. Takes effect on the next request; there is no cache."""
    api_key = (
        # Cross-workspace on purpose, bounded by the organization — same reason
        # as the list above.
        ApiKey.objects.unscoped()
        .filter(pk=api_key_id, workspace__organization=request.org)
        .select_related("workspace")
        .first()
    )
    if api_key is None:
        raise Http404("No such API key.")

    if revoke_api_key(api_key):
        messages.success(request, f"Revoked “{api_key.name}”.")
    else:
        messages.info(request, f"“{api_key.name}” was already revoked.")
    return redirect(reverse("settings_org_api_keys"))
