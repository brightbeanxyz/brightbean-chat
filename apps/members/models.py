"""Memberships and invitations.

Ported from BrightBean Studio's ``apps/members/models.py``. Roles, levels and
the permission matrix live in :mod:`apps.members.roles` (deviation 6); Studio's
``CustomRole`` is dropped entirely (deviation 5).

Fixes carried over the port:

* ``OrgMembership.accepted_at`` is actually written. Studio declares the column
  and never sets it, so "has this person accepted?" is unanswerable from the
  membership row.
* Indexes on the invitation lookups the service layer performs on every invite.
"""

import secrets

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.common.managers import OrgScopedManager
from apps.common.models import BaseModel
from apps.members.roles import OrgRole, WorkspaceRole, permissions_for_role

INVITE_TOKEN_BYTES = 32


def generate_invitation_token() -> str:
    """A 43-character URL-safe token.

    Module-level and public because migrations record it by import path; moving
    or renaming it breaks ``0001_initial``.
    """
    return secrets.token_urlsafe(INVITE_TOKEN_BYTES)


class OrgMembership(BaseModel):
    """A user's place in an organization."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="org_memberships",
    )
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    org_role = models.CharField(max_length=20, choices=OrgRole.choices, default=OrgRole.MEMBER)
    invited_at = models.DateTimeField(auto_now_add=True)
    accepted_at = models.DateTimeField(blank=True, null=True)

    objects = OrgScopedManager()

    class Meta:
        db_table = "members_org_membership"
        constraints = [
            models.UniqueConstraint(fields=["user", "organization"], name="orgmembership_unique_user_org"),
        ]

    def __str__(self) -> str:
        return f"{self.user.email} - {self.organization.name} ({self.org_role})"


class WorkspaceMembership(BaseModel):
    """A user's role inside one workspace."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="workspace_memberships",
    )
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    workspace_role = models.CharField(
        max_length=20,
        choices=WorkspaceRole.choices,
        default=WorkspaceRole.VIEWER,
    )
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "members_workspace_membership"
        constraints = [
            models.UniqueConstraint(fields=["user", "workspace"], name="workspacemembership_unique_user_workspace"),
        ]

    def __str__(self) -> str:
        return f"{self.user.email} - {self.workspace.name} ({self.workspace_role})"

    @property
    def effective_permissions(self) -> dict[str, bool]:
        """The single permission-resolution point, and the sole protocol.

        ``require_permission`` reads nothing else, which is what lets the public
        API (#25) authorize a bearer token by duck-typing an object with just
        this attribute against the same decorator — exactly as Studio's
        ``apps/api/auth.py`` does with its ``VirtualMembership``. Keep this the
        only way a permission decision is made: anything that reads
        ``workspace_role`` directly is a second, divergent answer waiting to
        happen.
        """
        return permissions_for_role(self.workspace_role)


class Invitation(BaseModel):
    """A pending invitation into an organization, with workspace assignments.

    The token is a random 256-bit value in a database row rather than a signed
    payload from ``apps.common.signing``: an invitation must be revocable and
    single-use, and both of those are state. SECURITY-BASELINE §4's shared
    signer is for *stateless* public token routes (``/u/``, ``/c/``, ``/o/``,
    ``/internal/tick``), where there is nothing to revoke.

    Revoking sets ``expires_at`` to now rather than adding a status column, so
    every consumer only has to understand one expiry rule.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="invitations",
    )
    email = models.EmailField()
    org_role = models.CharField(max_length=20, choices=OrgRole.choices, default=OrgRole.MEMBER)
    workspace_assignments = models.JSONField(
        default=list,
        help_text='List of {"workspace_id": "...", "role": "..."}',
    )
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="sent_invitations",
    )
    token = models.CharField(max_length=255, unique=True, default=generate_invitation_token)
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(blank=True, null=True)

    objects = OrgScopedManager()

    class Meta:
        db_table = "members_invitation"
        ordering = ["-created_at"]
        indexes = [
            # create_invitation looks for a pending invite on every send.
            models.Index(fields=["organization", "email"], name="invitation_org_email_idx"),
        ]

    def __str__(self) -> str:
        return f"Invitation to {self.email} for {self.organization.name}"

    @property
    def is_expired(self) -> bool:
        return timezone.now() > self.expires_at

    @property
    def is_accepted(self) -> bool:
        return self.accepted_at is not None

    @property
    def is_pending(self) -> bool:
        return not self.is_accepted and not self.is_expired
