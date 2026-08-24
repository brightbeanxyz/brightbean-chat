"""Object builders shared by every test module.

BrightBean Studio copy-pastes a ``_make_user()`` into three test modules, each
of which creates a user and then *deletes* the organization, workspace and
membership that its ``post_save`` provisioning signal just made. That helper
does not exist here: provisioning happens at signup and on first visit, never on
``User.objects.create_user`` (see ``apps.accounts.services``), so a test that
wants a bare user just makes one.

Plain ``Model.objects.create`` throughout, matching the root ``conftest.py`` and
Studio's convention — no factory_boy.
"""

from dataclasses import dataclass, field
from typing import Any

from django.contrib.auth import get_user_model

from apps.members.models import OrgMembership, WorkspaceMembership
from apps.members.roles import OrgRole, WorkspaceRole
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace

TEST_PASSWORD = "correct-horse-battery-staple"  # noqa: S105 - a test fixture, grants nothing


def create_user(email: str, **extra: Any) -> Any:
    """A user and nothing else — no organization, no workspace."""
    return get_user_model().objects.create_user(email=email, password=TEST_PASSWORD, **extra)


@dataclass
class Tenancy:
    """One organization, one workspace, and a user holding each role.

    ``owner`` is the org owner and a workspace admin. ``members`` maps each
    ``WorkspaceRole`` value to a user who holds exactly that role in
    ``workspace`` and is an ordinary org member.
    """

    slug: str
    organization: Organization
    workspace: Workspace
    owner: Any
    members: dict[str, Any] = field(default_factory=dict)

    def user_for(self, role: str) -> Any:
        return self.members[role]

    def membership_for(self, role: str) -> WorkspaceMembership:
        return WorkspaceMembership.objects.get(user=self.members[role], workspace=self.workspace)

    @property
    def org_membership(self) -> OrgMembership:
        return OrgMembership.objects.get(user=self.owner, organization=self.organization)


def create_tenancy(slug: str, *, workspace_name: str | None = None) -> Tenancy:
    """Build a complete tenant: org, workspace, owner and one user per role."""
    organization = Organization.objects.create(name=f"{slug} org")
    workspace = Workspace.objects.create(organization=organization, name=workspace_name or f"{slug} workspace")

    owner = create_user(f"owner@{slug}.test")
    OrgMembership.objects.create(user=owner, organization=organization, org_role=OrgRole.OWNER)
    WorkspaceMembership.objects.create(user=owner, workspace=workspace, workspace_role=WorkspaceRole.ADMIN)
    owner.last_workspace_id = workspace.pk
    owner.save(update_fields=["last_workspace_id"])

    members: dict[str, Any] = {}
    for role in WorkspaceRole:
        user = create_user(f"{role.value}@{slug}.test")
        OrgMembership.objects.create(user=user, organization=organization, org_role=OrgRole.MEMBER)
        WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role=role.value)
        user.last_workspace_id = workspace.pk
        user.save(update_fields=["last_workspace_id"])
        members[role.value] = user

    return Tenancy(slug=slug, organization=organization, workspace=workspace, owner=owner, members=members)


def email_identity(workspace: Any, connection: Any, address: str) -> Any:
    """A contact with an opted-in email identity on ``connection``.

    Lives here rather than beside either caller because both
    ``apps/channels/tests/test_email_suppression.py`` and
    ``tests/acceptance/test_unsubscribe_round_trip.py`` need exactly this, and a
    second copy drifts the moment ``ContactChannelIdentity`` gains a field —
    quietly building an identity the product no longer produces. Same rule
    ``tests/ssrf.py`` states for its own consolidation.

    Imports are function-local: this module is imported by suites running in
    deployments that may not have messaging installed, and a top-level import
    would make it a hard dependency of every one of them.
    """
    from django.utils import timezone

    from apps.common.platforms import Platform
    from apps.contacts.services import create_contact
    from apps.messaging.models import ContactChannelIdentity

    contact = create_contact(workspace, source="manual", email=address)
    return ContactChannelIdentity.objects.create(
        contact=contact,
        channel_connection=connection,
        platform=Platform.EMAIL.value,
        platform_user_id=address,
        opt_in=True,
        opt_in_at=timezone.now(),
        opt_in_source="data_collection",
    )
