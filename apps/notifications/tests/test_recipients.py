"""Who a notification actually reaches.

This is the issue's first acceptance criterion ("notify() with roles resolves
Admin members only") and the place a plausible-looking implementation is most
likely to be quietly wrong — see
:meth:`TestOrgOwners.test_an_org_owner_with_no_workspace_membership_is_an_admin`.
"""

import pytest

from apps.members.models import OrgMembership, WorkspaceMembership
from apps.members.roles import OrgRole, WorkspaceRole
from apps.notifications.recipients import active_users, recipients_for_roles
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace
from tests.support import create_user


@pytest.mark.django_db
class TestRoleResolution:
    def test_admin_resolves_the_owner_and_the_workspace_admin_only(self, tenancy):
        people = recipients_for_roles(tenancy.workspace, (WorkspaceRole.ADMIN,))

        assert set(people) == {tenancy.owner, tenancy.members["admin"]}

    def test_editor_agent_and_viewer_are_not_admins(self, tenancy):
        people = recipients_for_roles(tenancy.workspace, (WorkspaceRole.ADMIN,))

        for role in ("editor", "agent", "viewer"):
            assert tenancy.members[role] not in people

    def test_a_role_resolves_exactly_its_own_holders(self, tenancy):
        assert recipients_for_roles(tenancy.workspace, (WorkspaceRole.EDITOR,)) == [tenancy.members["editor"]]
        assert recipients_for_roles(tenancy.workspace, (WorkspaceRole.VIEWER,)) == [tenancy.members["viewer"]]

    def test_several_roles_union_without_duplicating(self, tenancy):
        people = recipients_for_roles(tenancy.workspace, (WorkspaceRole.ADMIN, WorkspaceRole.EDITOR))

        assert len(people) == len(set(people))
        assert set(people) == {tenancy.owner, tenancy.members["admin"], tenancy.members["editor"]}

    def test_another_tenants_admin_is_never_resolved(self, tenancy, other_tenancy):
        people = recipients_for_roles(tenancy.workspace, (WorkspaceRole.ADMIN,))

        assert other_tenancy.owner not in people
        assert other_tenancy.members["admin"] not in people

    def test_a_deactivated_admin_is_skipped(self, tenancy):
        admin = tenancy.members["admin"]
        admin.is_active = False
        admin.save(update_fields=["is_active"])

        assert admin not in recipients_for_roles(tenancy.workspace, (WorkspaceRole.ADMIN,))

    def test_an_unknown_role_raises_rather_than_resolving_nobody(self, tenancy):
        """Silently returning [] would look identical to "this workspace has no
        admins", and the caller would never learn it had a typo."""
        with pytest.raises(ValueError, match="Unknown workspace role"):
            recipients_for_roles(tenancy.workspace, ("superadmin",))

    def test_the_result_is_ordered_so_callers_can_rely_on_it(self, tenancy):
        people = recipients_for_roles(tenancy.workspace, (WorkspaceRole.ADMIN, WorkspaceRole.EDITOR))

        assert [p.email for p in people] == sorted(p.email for p in people)


@pytest.mark.django_db
class TestOrgOwners:
    """SPEC §4.2: an org owner is a workspace admin in every workspace of their org."""

    def test_an_org_owner_with_no_workspace_membership_is_an_admin(self, tenancy):
        """The test that actually exercises §4.2.

        ``create_tenancy`` gives its owner an explicit ``WorkspaceRole.ADMIN``
        membership, so the obvious assertion above passes even for an
        implementation that only ever asks ``WorkspaceMembership`` — right
        answer, wrong reason. This owner holds no workspace row at all, which is
        the ordinary state of a solo founder who never added themselves to their
        own workspace, and is exactly who the loop-cap alert has to reach.
        """
        second_owner = create_user(f"owner2@{tenancy.slug}.test")
        OrgMembership.objects.create(user=second_owner, organization=tenancy.organization, org_role=OrgRole.OWNER)
        assert not WorkspaceMembership.objects.filter(user=second_owner).exists()

        assert second_owner in recipients_for_roles(tenancy.workspace, (WorkspaceRole.ADMIN,))

    def test_an_org_admin_gets_nothing_without_a_workspace_membership(self, tenancy):
        """The negative half. An org *admin* is bounded by actual membership —
        only owners get implicit workspace authority."""
        org_admin = create_user(f"orgadmin@{tenancy.slug}.test")
        OrgMembership.objects.create(user=org_admin, organization=tenancy.organization, org_role=OrgRole.ADMIN)

        assert org_admin not in recipients_for_roles(tenancy.workspace, (WorkspaceRole.ADMIN,))

    def test_owners_are_not_folded_into_non_admin_roles(self, tenancy):
        """An owner *is* an admin, not a stand-in for every role. Folding them
        into roles=("viewer",) would mail owners about viewer-targeted notices."""
        second_owner = create_user(f"owner3@{tenancy.slug}.test")
        OrgMembership.objects.create(user=second_owner, organization=tenancy.organization, org_role=OrgRole.OWNER)

        assert second_owner not in recipients_for_roles(tenancy.workspace, (WorkspaceRole.VIEWER,))

    def test_an_owner_of_another_org_is_not_an_admin_here(self, tenancy):
        other_org = Organization.objects.create(name="unrelated org")
        other_workspace = Workspace.objects.create(organization=other_org, name="unrelated workspace")
        stranger = create_user("stranger@unrelated.test")
        OrgMembership.objects.create(user=stranger, organization=other_org, org_role=OrgRole.OWNER)

        assert stranger in recipients_for_roles(other_workspace, (WorkspaceRole.ADMIN,))
        assert stranger not in recipients_for_roles(tenancy.workspace, (WorkspaceRole.ADMIN,))


@pytest.mark.django_db
class TestExplicitRecipients:
    def test_duplicates_collapse(self, tenancy):
        """The same person can be both the assignee and a mentioned member;
        that is one notification, not two."""
        admin = tenancy.members["admin"]

        assert active_users([admin, admin, tenancy.members["editor"]]) == [admin, tenancy.members["editor"]]

    def test_deactivated_and_none_are_dropped(self, tenancy):
        viewer = tenancy.members["viewer"]
        viewer.is_active = False
        viewer.save(update_fields=["is_active"])

        assert active_users([None, viewer, tenancy.owner]) == [tenancy.owner]
