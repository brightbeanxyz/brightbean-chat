"""Member management, org-scoped.

Mounted at ``/organization/members/``: memberships and invitations belong to the
organization, and one invitation can carry assignments for several workspaces at
once. Authority is checked twice and at different levels — ``require_org_role``
gates the pages, and :mod:`apps.members.services` gates each individual
workspace assignment on ``manage_members`` *in that workspace*. That second
check is what the SPEC §4 permission table is for: an Editor holds no
``manage_members`` anywhere, so an Editor cannot manage members.

Plain form posts and redirects rather than Studio's HTMX fragments: HTMX arrives
with issue #32, and building against a library this branch does not vendor would
ship a page that silently does nothing. #32 restyles and can convert these.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from apps.members.decorators import require_org_role
from apps.members.models import Invitation, OrgMembership, WorkspaceMembership
from apps.members.requests import OrgRequest
from apps.members.roles import ORG_ROLE_LEVEL, OrgRole, WorkspaceRole
from apps.members.services import (
    MembershipError,
    create_invitation,
    manageable_workspaces,
    may_manage_org_membership,
    org_level,
    remove_member,
    resend_invitation,
    revoke_invitation,
    update_member_org_role,
    update_workspace_assignments,
)
from apps.members.signals_keys import PENDING_INVITE_SESSION_KEY
from apps.workspaces.models import Workspace


def _org_role_choices_for(membership: OrgMembership) -> list[tuple[str, str]]:
    """Only offer roles the backend would actually accept.

    Mirrors ``create_invitation``'s admin-tier rule, so the form never presents
    an option that comes back as an error.
    """
    choices: list[tuple[str, str]] = []
    if membership.org_role == OrgRole.OWNER:
        choices.append((OrgRole.ADMIN.value, "Admin"))
    choices.append((OrgRole.MEMBER.value, "Member"))
    return choices


def _parse_assignments(request: OrgRequest, workspaces: list[Workspace]) -> list[dict[str, str]]:
    """Read the ``ws_<id>`` / ``ws_role_<id>`` field pairs off a POST."""
    assignments = []
    for workspace in workspaces:
        if request.POST.get(f"ws_{workspace.pk}"):
            assignments.append(
                {
                    "workspace_id": str(workspace.pk),
                    "role": request.POST.get(f"ws_role_{workspace.pk}") or WorkspaceRole.VIEWER.value,
                }
            )
    return assignments


@login_required
@require_org_role("member")
@require_GET
def member_list(request: OrgRequest) -> HttpResponse:
    memberships = OrgMembership.objects.for_org(request.org.pk).select_related("user").order_by("user__email")
    invitations = Invitation.objects.for_org(request.org.pk).filter(accepted_at__isnull=True)
    workspaces = manageable_workspaces(request.user, request.org)
    caller_level = org_level(request.user, request.org)
    # Resolved per row rather than "anyone but me", so the form never presents a
    # role change or a removal that the service layer will refuse — which is
    # what _org_role_choices_for exists for one level up.
    rows = [
        {
            "membership": membership,
            "can_manage": membership.user_id != request.user.pk
            and may_manage_org_membership(caller_level, membership.org_role),
        }
        for membership in memberships
    ]
    return render(
        request,
        "members/list.html",
        {
            "rows": rows,
            "invitations": [i for i in invitations if not i.is_expired],
            "workspaces": workspaces,
            "workspace_roles": WorkspaceRole.choices,
            "org_role_choices": _org_role_choices_for(request.org_membership),
            "is_admin": caller_level >= ORG_ROLE_LEVEL[OrgRole.ADMIN],
        },
    )


@login_required
@require_org_role("admin")
@require_POST
def invite_member(request: OrgRequest) -> HttpResponse:
    workspaces = manageable_workspaces(request.user, request.org)
    try:
        create_invitation(
            org=request.org,
            email=request.POST.get("email", ""),
            org_role=request.POST.get("org_role", OrgRole.MEMBER.value),
            workspace_assignments=_parse_assignments(request, workspaces),
            invited_by=request.user,
        )
    except MembershipError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Invitation sent.")
    return redirect(reverse("members:list"))


@login_required
@require_org_role("admin")
@require_POST
def resend_invite(request: OrgRequest, invitation_id: str) -> HttpResponse:
    invitation = get_object_or_404(Invitation, pk=invitation_id, organization=request.org)
    try:
        resend_invitation(invitation)
    except MembershipError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Invitation resent.")
    return redirect(reverse("members:list"))


@login_required
@require_org_role("admin")
@require_POST
def revoke_invite(request: OrgRequest, invitation_id: str) -> HttpResponse:
    invitation = get_object_or_404(Invitation, pk=invitation_id, organization=request.org)
    try:
        revoke_invitation(invitation)
    except MembershipError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Invitation revoked.")
    return redirect(reverse("members:list"))


@login_required
@require_org_role("admin")
@require_POST
def update_member_role(request: OrgRequest, membership_id: str) -> HttpResponse:
    membership = get_object_or_404(OrgMembership, pk=membership_id, organization=request.org)
    try:
        update_member_org_role(request.org, membership, request.POST.get("org_role", ""), caller=request.user)
    except MembershipError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Role updated.")
    return redirect(reverse("members:list"))


@login_required
@require_org_role("admin")
@require_POST
def remove_member_view(request: OrgRequest, membership_id: str) -> HttpResponse:
    membership = get_object_or_404(OrgMembership, pk=membership_id, organization=request.org)
    try:
        remove_member(request.org, membership, request.user)
    except MembershipError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Member removed.")
    return redirect(reverse("members:list"))


@login_required
@require_org_role("member")
def manage_workspaces(request: OrgRequest, membership_id: str) -> HttpResponse:
    """Set one member's workspace roles.

    Open to any org member because workspace authority is not an org role: a
    workspace Admin who is only an org *member* must be able to manage their own
    workspace's people. Only workspaces the caller actually has
    ``manage_members`` in are shown, and the service re-checks every assignment
    — the rendered form is a convenience, never the control.
    """
    membership = get_object_or_404(OrgMembership, pk=membership_id, organization=request.org)
    workspaces = manageable_workspaces(request.user, request.org)

    if request.method == "POST":
        try:
            update_workspace_assignments(
                request.org,
                membership.user,
                _parse_assignments(request, workspaces),
                inviter=request.user,
            )
        except MembershipError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, "Workspace access updated.")
        return redirect(reverse("members:manage_workspaces", kwargs={"membership_id": membership_id}))

    current = {
        str(m.workspace_id): m.workspace_role
        for m in WorkspaceMembership.objects.filter(user=membership.user, workspace__organization=request.org)
    }
    # Resolved here rather than in the template: matching a UUID against a dict
    # key is the kind of thing Django's template language can only do badly.
    rows = [
        {
            "workspace": workspace,
            "selected": str(workspace.pk) in current,
            "role": current.get(str(workspace.pk), WorkspaceRole.VIEWER.value),
        }
        for workspace in workspaces
    ]
    return render(
        request,
        "members/manage_workspaces.html",
        {
            "membership": membership,
            "rows": rows,
            "workspace_roles": WorkspaceRole.choices,
        },
    )


def accept_invite(request: HttpRequest, token: str) -> HttpResponse:
    """The public invite landing page.

    Unauthenticated on purpose — the token is the credential. An unknown,
    expired or already-accepted token renders the same terminal page, so the URL
    does not report which invitations exist.
    """
    invitation = Invitation.objects.filter(token=token).select_related("organization").first()
    if invitation is None or invitation.is_accepted or invitation.is_expired:
        return render(request, "members/invite_expired.html", status=404)

    if not request.user.is_authenticated:
        # Stash the token so the signup flow can prefill the address and the
        # user_signed_up receiver can join the right organization.
        request.session[PENDING_INVITE_SESSION_KEY] = token
        return render(request, "members/accept_invite.html", {"invitation": invitation, "needs_login": True})

    if request.method == "POST":
        from apps.members.services import accept_invitation

        try:
            accept_invitation(invitation, request.user)
        except MembershipError as exc:
            return render(
                request,
                "members/accept_invite.html",
                {"invitation": invitation, "error": str(exc)},
                status=422,
            )
        messages.success(request, f"You have joined {invitation.organization.name}.")
        return redirect(reverse("index"))

    return render(request, "members/accept_invite.html", {"invitation": invitation})
