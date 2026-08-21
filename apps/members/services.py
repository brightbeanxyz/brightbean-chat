"""Invitation and membership lifecycle.

Ported from BrightBean Studio's ``apps/members/services.py``. The role-level
maps are imported from :mod:`apps.members.roles` rather than re-declared with a
"must match decorators.py" comment (deviation 6).

Two rules define what anyone may do here:

* **Org tier.** Only an owner can create another admin. An admin inviting an
  admin is a lateral privilege clone, which is exactly what a compromised admin
  account wants; member-tier invites stay open so a workspace admin who is only
  an org *member* can still bring collaborators in.
* **Workspace tier.** Authority over a workspace assignment requires
  ``manage_members`` **in that workspace** — or org ownership, which confers
  workspace-admin authority everywhere in the org. This is where the
  ``manage_members`` permission key from SPEC §4 actually binds: an Editor
  cannot manage members, because Editor does not hold the key.

Both directions are checked. Studio learned the second one the hard way: without
a check against the *existing* role, a low-authority admin could demote an owner
simply by submitting the assignment form with ``role=viewer`` — the requested
level passes a "not higher than mine" test, and the demotion of the owner row is
the privilege violation.
"""

import logging
import smtplib
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from apps.members.models import Invitation, OrgMembership, WorkspaceMembership
from apps.members.roles import ORG_ROLE_LEVEL, WORKSPACE_ROLE_LEVEL, OrgRole, WorkspaceRole, permissions_for_role
from apps.workspaces.models import Workspace

logger = logging.getLogger(__name__)

INVITE_EXPIRY_DAYS = 7


class MembershipError(ValueError):
    """A membership operation the caller is not allowed to perform.

    A ``ValueError`` subclass so views can keep catching ``ValueError`` and
    rendering the message inline, while callers that care can be specific.
    """


@dataclass(frozen=True)
class WorkspaceAssignment:
    workspace_id: str
    role: str


# ---------------------------------------------------------------------------
# Authority
# ---------------------------------------------------------------------------


def org_level(user: Any, org: Any) -> int:
    """The user's authority level in ``org``; 0 if they are not a member."""
    membership = OrgMembership.objects.filter(user=user, organization=org).first()
    if not membership:
        return 0
    return ORG_ROLE_LEVEL.get(membership.org_role, 0)


def workspace_authority_map(user: Any, org: Any) -> dict[str, int]:
    """Workspace id → the most senior role ``user`` may grant, change or remove there.

    A missing key means no authority at all, which is the answer for every role
    without ``manage_members`` — Editor, Agent and Viewer. Org owners get
    workspace-admin authority across the org, mirroring the fact that they can
    already reach everything through org settings; org *admins* get no implicit
    workspace authority, only what their own membership grants.

    Built as a map rather than answered one workspace at a time because the
    callers ask about every workspace in the org — rendering the member form,
    validating a submitted assignment set — and a per-workspace lookup made that
    two queries per workspace. This is two queries total, whatever the count.
    """
    if org_level(user, org) >= ORG_ROLE_LEVEL[OrgRole.OWNER]:
        admin_level = WORKSPACE_ROLE_LEVEL[WorkspaceRole.ADMIN]
        live = Workspace.objects.for_org(org.pk).filter(is_archived=False)
        return {str(pk): admin_level for pk in live.values_list("id", flat=True)}

    memberships = WorkspaceMembership.objects.filter(
        user=user, workspace__organization=org, workspace__is_archived=False
    )
    return {
        str(membership.workspace_id): WORKSPACE_ROLE_LEVEL.get(membership.workspace_role, 0)
        for membership in memberships
        if membership.effective_permissions.get("manage_members", False)
    }


def workspace_authority_level(user: Any, org: Any, workspace_id: Any) -> int:
    """Single-workspace form of :func:`workspace_authority_map`."""
    return workspace_authority_map(user, org).get(str(workspace_id), 0)


def manageable_workspaces(user: Any, org: Any) -> list[Workspace]:
    """The live workspaces ``user`` may assign roles in.

    The member-management form renders exactly this list, and
    :func:`update_workspace_assignments` treats exactly this list as the set a
    submission speaks for — the two have to agree, or omission-means-removal
    starts removing memberships the caller was never shown.
    """
    authority = workspace_authority_map(user, org)
    if not authority:
        return []
    return list(Workspace.objects.for_org(org.pk).filter(is_archived=False, id__in=list(authority)))


def may_manage_org_membership(caller_level: int, target_role: str) -> bool:
    """Whether a caller may change or remove a membership at ``target_role``.

    Admin and above are owner-only, mirroring ``create_invitation``'s rule that
    only an owner may *create* an admin. Without the mirror the privilege is
    destroyable but not restorable: one admin could demote or remove another,
    and no admin could put them back.
    """
    target_level = ORG_ROLE_LEVEL.get(target_role, 0)
    if target_level >= ORG_ROLE_LEVEL[OrgRole.ADMIN]:
        return caller_level >= ORG_ROLE_LEVEL[OrgRole.OWNER]
    return caller_level >= target_level


def _assert_may_manage_org_membership(caller_level: int, target_role: str, verb: str) -> None:
    if may_manage_org_membership(caller_level, target_role):
        return
    if caller_level < ORG_ROLE_LEVEL.get(target_role, 0):
        raise MembershipError(f"You cannot {verb} a member whose role is higher than your own.")
    raise MembershipError(f"Only organization owners can {verb} an organization admin.")


def _normalise_assignments(org: Any, assignments: Any) -> list[WorkspaceAssignment]:
    """Validate raw assignment dicts against the org's live workspaces."""
    workspace_ids = {
        str(pk) for pk in Workspace.objects.for_org(org.pk).filter(is_archived=False).values_list("id", flat=True)
    }
    cleaned: list[WorkspaceAssignment] = []
    for entry in assignments or []:
        workspace_id = str(entry.get("workspace_id") or "")
        role = str(entry.get("role") or "")
        if workspace_id not in workspace_ids:
            raise MembershipError("That workspace does not belong to your organization.")
        if role not in WORKSPACE_ROLE_LEVEL:
            raise MembershipError(f"Unknown workspace role: {role!r}.")
        cleaned.append(WorkspaceAssignment(workspace_id=workspace_id, role=role))
    return cleaned


def _assert_may_grant(authority: dict[str, int], assignments: list[WorkspaceAssignment]) -> None:
    for assignment in assignments:
        level = authority.get(assignment.workspace_id, 0)
        if level == 0:
            raise MembershipError("You cannot manage members in that workspace.")
        if WORKSPACE_ROLE_LEVEL[assignment.role] > level:
            raise MembershipError("You cannot grant a workspace role higher than your own in that workspace.")


def _assert_not_last_workspace_admin(workspace_id: Any, *, excluding: Any) -> None:
    """Refuse to leave a workspace with no admin.

    An org owner could still reach it, but nobody inside the workspace could
    manage its channels, settings or members — so the workspace quietly becomes
    read-only for the people who work in it.
    """
    remaining = (
        WorkspaceMembership.objects.filter(workspace_id=workspace_id, workspace_role=WorkspaceRole.ADMIN)
        .exclude(pk=excluding)
        .exists()
    )
    if not remaining:
        raise MembershipError("Cannot remove the last admin of a workspace. Promote someone else first.")


# ---------------------------------------------------------------------------
# Invitations
# ---------------------------------------------------------------------------


def create_invitation(
    org: Any,
    email: str,
    org_role: str,
    workspace_assignments: Any,
    invited_by: Any,
    *,
    inviter: Any = None,
) -> Invitation:
    """Invite ``email`` into ``org``. Raises ``MembershipError`` on any rule."""
    email = (email or "").strip().lower()
    if not email:
        raise MembershipError("An email address is required.")

    if OrgMembership.objects.filter(organization=org, user__email__iexact=email).exists():
        raise MembershipError("This person is already a member of your organization.")

    existing = Invitation.objects.for_org(org.pk).filter(email=email, accepted_at__isnull=True).first()
    if existing and not existing.is_expired:
        raise MembershipError("An invitation is already pending for this address. You can resend it instead.")

    if org_role == OrgRole.OWNER:
        raise MembershipError("Cannot invite someone as an organization owner.")

    requested_level = ORG_ROLE_LEVEL.get(org_role, 0)
    if requested_level == 0:
        raise MembershipError(f"Unknown org role: {org_role!r}.")

    effective_inviter = inviter or invited_by
    inviter_level = org_level(effective_inviter, org)
    if requested_level >= ORG_ROLE_LEVEL[OrgRole.ADMIN] and requested_level >= inviter_level:
        raise MembershipError("Only organization owners can invite someone as an admin.")

    assignments = _normalise_assignments(org, workspace_assignments)
    if inviter_level < ORG_ROLE_LEVEL[OrgRole.ADMIN] and not assignments:
        # A workspace Admin who is only an org member invites *into their
        # workspace*; their authority is the workspace, so an invitation that
        # names none would be adding someone to the organization on no
        # authority at all.
        raise MembershipError("Choose at least one workspace to invite this person into.")
    _assert_may_grant(workspace_authority_map(effective_inviter, org), assignments)

    invitation = Invitation(
        organization=org,
        email=email,
        org_role=org_role,
        workspace_assignments=[{"workspace_id": a.workspace_id, "role": a.role} for a in assignments],
        invited_by=invited_by,
        expires_at=timezone.now() + timedelta(days=INVITE_EXPIRY_DAYS),
    )
    token = invitation.issue_token()
    invitation.save()
    send_invite_email(invitation, token)
    return invitation


@transaction.atomic
def accept_invitation(invitation: Invitation, user: Any, *, require_email_match: bool = True) -> None:
    """Turn a pending invitation into memberships.

    Atomic, unlike Studio's: it writes an org membership, N workspace
    memberships and the acceptance stamp, and a failure part-way through would
    leave someone in an org with no workspace and an invitation that still looks
    pending.

    Single-use is enforced under a row lock. The caller looked the invitation up
    before the transaction opened, so validating that stale instance would let
    two concurrent acceptances of the same token both see ``accepted_at=None``,
    both create memberships — for *different* users, since the signup path
    disables the email check — and both stamp it accepted.

    ``require_email_match=False`` is for the signup path, where the session-bound
    token is itself proof the invite reached its recipient — and where a social
    login returns whatever address the provider owns, which need not be the one
    invited.
    """
    locked = Invitation.objects.select_for_update().filter(pk=invitation.pk).select_related("organization").first()
    if locked is None:
        raise MembershipError("This invitation is no longer available.")
    invitation = locked

    if invitation.is_accepted:
        raise MembershipError("This invitation has already been accepted.")
    if invitation.is_expired:
        raise MembershipError("This invitation has expired.")
    if require_email_match and (user.email or "").strip().lower() != invitation.email.strip().lower():
        raise MembershipError("This invitation was sent to a different email address.")

    # v1 routes org-scoped pages from a single OrgMembership (see
    # RBACMiddleware). A second one would leave request.org and
    # last_workspace_id pointing at different organizations — every page
    # rendering one org's members while the workspace switcher shows another's.
    # Refusing is the only coherent answer until multi-org lands.
    other = OrgMembership.objects.filter(user=user).exclude(organization=invitation.organization).first()
    if other is not None:
        raise MembershipError(
            f"This account already belongs to {other.organization.name}. "
            "BrightBean Chat supports one organization per account, so leave that one first "
            "or accept this invitation from a different account."
        )

    OrgMembership.objects.get_or_create(
        user=user,
        organization=invitation.organization,
        defaults={"org_role": invitation.org_role, "accepted_at": timezone.now()},
    )

    first_workspace_id = None
    for assignment in invitation.workspace_assignments or []:
        workspace_id = assignment.get("workspace_id")
        role = assignment.get("role")
        if not workspace_id or role not in WORKSPACE_ROLE_LEVEL:
            continue
        if not Workspace.objects.for_org(invitation.organization_id).filter(pk=workspace_id).exists():
            continue
        WorkspaceMembership.objects.get_or_create(
            user=user,
            workspace_id=workspace_id,
            defaults={"workspace_role": role},
        )
        first_workspace_id = first_workspace_id or workspace_id

    invitation.accepted_at = timezone.now()
    invitation.save(update_fields=["accepted_at", "updated_at"])

    if first_workspace_id:
        user.last_workspace_id = first_workspace_id
        user.save(update_fields=["last_workspace_id"])


def resend_invitation(invitation: Invitation) -> Invitation:
    """Mint a fresh token and expiry, then re-send.

    Rotating the token means an old link stops working, which is what makes
    "resend" also a repair for a leaked or stale invite.
    """
    if invitation.is_accepted:
        raise MembershipError("Cannot resend an already accepted invitation.")
    token = invitation.issue_token()
    invitation.expires_at = timezone.now() + timedelta(days=INVITE_EXPIRY_DAYS)
    invitation.save(update_fields=["token_digest", "expires_at", "updated_at"])
    send_invite_email(invitation, token)
    return invitation


def revoke_invitation(invitation: Invitation) -> Invitation:
    """Revoke by expiring, so there is only one expiry rule to understand."""
    if invitation.is_accepted:
        raise MembershipError("Cannot revoke an already accepted invitation.")
    invitation.expires_at = timezone.now()
    invitation.save(update_fields=["expires_at", "updated_at"])
    return invitation


def send_invite_email(invitation: Invitation, token: str) -> None:
    """Send the invite. Delivery failures are logged, never raised.

    ``token`` is passed in rather than read off the invitation: the row holds
    only a digest, so the raw value exists for exactly as long as the request
    that minted it.

    A mail outage must not roll back a created invitation: the row is the
    durable thing, and "resend" exists precisely for this.
    """
    app_url = getattr(settings, "APP_URL", "http://localhost:8000").rstrip("/")
    accept_path = reverse("accept_invite", kwargs={"token": token})
    accept_url = f"{app_url}{accept_path}"
    context = {
        "invitation": invitation,
        "accept_url": accept_url,
        "org_name": invitation.organization.name,
        "invited_by": invitation.invited_by,
        "app_url": app_url,
    }
    try:
        message = EmailMultiAlternatives(
            subject=f"You have been invited to {invitation.organization.name} on BrightBean Chat",
            body=render_to_string("members/email/invite.txt", context),
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[invitation.email],
        )
        message.attach_alternative(render_to_string("members/email/invite.html", context), "text/html")
        message.send()
    except (OSError, smtplib.SMTPException):
        # Delivery only. A TemplateSyntaxError or a renamed context key must not
        # be reported as an SMTP problem and then silently swallowed — the
        # invitation would look sent and never arrive.
        logger.exception("Failed to send invitation email for invitation %s", invitation.pk)


# ---------------------------------------------------------------------------
# Membership changes
# ---------------------------------------------------------------------------


def remove_member(org: Any, membership: OrgMembership, removed_by: Any) -> None:
    """Remove a member from the org and every workspace in it."""
    if membership.user_id == removed_by.pk:
        raise MembershipError("You cannot remove yourself from the organization.")

    # Integrity before authority, deliberately. Only an owner outranks an owner,
    # and an owner removing the *last* owner can only be removing themselves —
    # which the check above already caught. Ordering it the other way would make
    # this branch unreachable, and an org admin who tries would be told "not
    # senior enough" rather than the thing they can actually act on. There is
    # nothing leaked by saying so: the member list they are looking at already
    # shows every role.
    if membership.org_role == OrgRole.OWNER:
        owners = OrgMembership.objects.filter(organization=org, org_role=OrgRole.OWNER).exclude(pk=membership.pk)
        if not owners.exists():
            raise MembershipError("Cannot remove the last organization owner.")

    _assert_may_manage_org_membership(org_level(removed_by, org), membership.org_role, "remove")

    with transaction.atomic():
        workspace_ids = list(Workspace.objects.for_org(org.pk).values_list("id", flat=True))
        memberships = WorkspaceMembership.objects.filter(user_id=membership.user_id, workspace_id__in=workspace_ids)

        # The same invariant update_workspace_assignments enforces. Removing
        # someone from the organization deletes their workspace memberships in
        # bulk, so without this the sole Admin of a workspace can be removed
        # through the members page and leave it with nobody who can manage its
        # settings, channels or people.
        for workspace_membership in memberships.filter(workspace_role=WorkspaceRole.ADMIN):
            _assert_not_last_workspace_admin(workspace_membership.workspace_id, excluding=workspace_membership.pk)

        memberships.delete()
        membership.delete()


def update_member_org_role(org: Any, membership: OrgMembership, new_role: str, *, caller: Any = None) -> OrgMembership:
    """Change someone's org role, within the caller's authority."""
    if new_role == OrgRole.OWNER:
        raise MembershipError("Cannot promote to owner. Transfer ownership instead.")

    new_level = ORG_ROLE_LEVEL.get(new_role, 0)
    if new_level == 0:
        raise MembershipError(f"Unknown org role: {new_role!r}.")

    if caller is not None:
        caller_level = org_level(caller, org)
        _assert_may_manage_org_membership(caller_level, membership.org_role, "change")
        if new_level >= ORG_ROLE_LEVEL[OrgRole.ADMIN] and new_level >= caller_level:
            raise MembershipError("Only organization owners can promote someone to admin.")

    if membership.org_role == OrgRole.OWNER:
        owners = OrgMembership.objects.filter(organization=org, org_role=OrgRole.OWNER).exclude(pk=membership.pk)
        if not owners.exists():
            raise MembershipError("Cannot change the role of the last organization owner.")

    membership.org_role = new_role
    membership.save(update_fields=["org_role", "updated_at"])
    return membership


@transaction.atomic
def update_workspace_assignments(org: Any, user: Any, assignments: Any, *, inviter: Any = None) -> None:
    """Set a member's workspace memberships to exactly ``assignments``.

    Omission means removal, so the caller must render every workspace **in
    scope** — and the scope is the caller's own authority, not the whole
    organization. Scoping it wider would make the form unsubmittable for anyone
    who manages some of the org's workspaces but not all of them: a membership
    in a workspace they were never shown is absent from ``desired``, reads as a
    deliberate removal, and fails the authority check below on every attempt.
    """
    authority = workspace_authority_map(inviter, org) if inviter is not None else None
    desired = {a.workspace_id: a.role for a in _normalise_assignments(org, assignments)}

    if authority is None:
        scope: set[str] = {str(pk) for pk in Workspace.objects.for_org(org.pk).values_list("id", flat=True)}
    else:
        scope = set(authority)

    current = {str(m.workspace_id): m for m in WorkspaceMembership.objects.filter(user=user, workspace_id__in=scope)}

    if authority is not None:
        # Authority over what is being granted...
        _assert_may_grant(authority, [WorkspaceAssignment(ws, role) for ws, role in desired.items()])
        # ...and over what is being taken away or changed. Without the second
        # half, a workspace admin could demote an owner-equivalent membership by
        # submitting a lower role: the requested level passes the first check,
        # and the demotion itself is the violation.
        for workspace_id, membership in current.items():
            existing_level = WORKSPACE_ROLE_LEVEL.get(membership.workspace_role, 0)
            level = authority.get(workspace_id, 0)
            changing = workspace_id not in desired or desired[workspace_id] != membership.workspace_role
            if changing and (level == 0 or existing_level > level):
                raise MembershipError("You cannot modify a workspace membership whose role is higher than your own.")

    for workspace_id, membership in current.items():
        if workspace_id in desired and desired[workspace_id] == membership.workspace_role:
            continue
        if membership.workspace_role == WorkspaceRole.ADMIN:
            _assert_not_last_workspace_admin(workspace_id, excluding=membership.pk)
        if workspace_id not in desired:
            membership.delete()
        else:
            membership.workspace_role = desired[workspace_id]
            membership.save(update_fields=["workspace_role", "updated_at"])

    for workspace_id, role in desired.items():
        if workspace_id not in current:
            WorkspaceMembership.objects.create(user=user, workspace_id=workspace_id, workspace_role=role)


def role_permission_table() -> dict[str, dict[str, bool]]:
    """The full role → permission mapping, for settings pages and docs."""
    return {role.value: permissions_for_role(role.value) for role in WorkspaceRole}
