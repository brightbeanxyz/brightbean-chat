"""The tenant root.

Ported from BrightBean Studio's ``apps/organizations/models.py``. Differences:
the pk and timestamps come from :class:`apps.common.models.BaseModel` (UUIDv7,
not a hand-rolled ``uuid.uuid4`` per model), and Studio's ``billing_email`` is
dropped — it belongs to its hosted-SaaS billing integration, which this project
does not have (SPEC §1.1: no billing).
"""

from typing import Any

from django.db import models

from apps.common.models import BaseModel


class Organization(BaseModel):
    """One tenant. Owns workspaces, memberships, invitations and credentials."""

    name = models.CharField(max_length=100)
    logo_url = models.URLField(blank=True, default="")
    default_timezone = models.CharField(max_length=63, default="UTC")

    # Deletion workflow: a request starts a grace period, a sweep (or an
    # immediate confirm) calls hard_delete. ``deleted_at`` is the tombstone for
    # a soft-delete path Studio declared but never wired; kept because issue #29
    # (GDPR) owns deletion semantics and will want somewhere to record it.
    deletion_requested_at = models.DateTimeField(blank=True, null=True)
    deletion_scheduled_for = models.DateTimeField(blank=True, null=True)
    deleted_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        db_table = "organizations_organization"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    @property
    def is_deletion_pending(self) -> bool:
        return self.deletion_requested_at is not None and self.deleted_at is None

    def hard_delete(self, requesting_user: Any = None) -> None:
        """Delete this org and settle every member's account.

        ``requesting_user`` (the person who confirmed an immediate delete) is
        deleted along with the org so re-signup is a genuinely fresh start.
        Every other member is re-provisioned with their own org and workspace,
        so they land on a working dashboard rather than in limbo. Pass ``None``
        from background sweeps, which carry no request context and must not
        delete accounts on their own initiative.

        Any user pointing at one of this org's workspaces has
        ``last_workspace_id`` cleared first: it is a bare UUID column with no
        foreign key, so nothing else would.
        """
        from apps.accounts.models import User
        from apps.accounts.services import provision_organization_and_workspace
        from apps.members.models import OrgMembership
        from apps.workspaces.models import Workspace

        member_ids = list(OrgMembership.objects.filter(organization=self).values_list("user_id", flat=True))
        workspace_ids = list(Workspace.objects.filter(organization=self).values_list("id", flat=True))

        if workspace_ids:
            User.objects.filter(last_workspace_id__in=workspace_ids).update(last_workspace_id=None)

        self.delete()  # CASCADE: workspaces, memberships, invitations, credentials

        requesting_user_id = requesting_user.pk if requesting_user else None
        for user_id in member_ids:
            if user_id == requesting_user_id:
                User.objects.filter(pk=user_id).delete()
                continue
            user = User.objects.filter(pk=user_id).first()
            if user is not None:
                provision_organization_and_workspace(user)
