"""Organization deletion and its aftermath."""

import pytest

from apps.accounts.models import User
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.members.roles import OrgRole
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace


@pytest.mark.django_db
class TestHardDelete:
    def test_it_removes_the_org_and_its_workspaces(self, tenancy):
        tenancy.organization.hard_delete()

        assert not Organization.objects.filter(pk=tenancy.organization.pk).exists()
        assert not Workspace.objects.filter(pk=tenancy.workspace.pk).exists()

    def test_surviving_members_are_re_provisioned(self, tenancy):
        """Otherwise they log in to an account that belongs to nothing."""
        tenancy.organization.hard_delete()

        for user in [tenancy.owner, *tenancy.members.values()]:
            membership = OrgMembership.objects.get(user=user)
            assert membership.org_role == OrgRole.OWNER
            assert WorkspaceMembership.objects.filter(user=user).exists()

    def test_the_requesting_user_is_deleted_with_it(self, tenancy):
        """The person who confirmed an immediate delete gets a fresh start on
        re-signup rather than a phantom organization."""
        tenancy.organization.hard_delete(requesting_user=tenancy.owner)

        assert not User.objects.filter(pk=tenancy.owner.pk).exists()

    def test_background_sweeps_delete_nobody(self, tenancy):
        """requesting_user=None is the scheduled path; it carries no request
        context and must not delete accounts on its own initiative."""
        tenancy.organization.hard_delete()

        assert User.objects.filter(pk=tenancy.owner.pk).exists()

    def test_dangling_last_workspace_ids_are_cleared(self, tenancy):
        """last_workspace_id is a bare UUID with no foreign key, so nothing
        else would."""
        stale = tenancy.workspace.pk

        tenancy.organization.hard_delete()

        for user in [tenancy.owner, *tenancy.members.values()]:
            user.refresh_from_db()
            assert user.last_workspace_id != stale

    def test_another_organization_is_untouched(self, tenancy, other_tenancy):
        tenancy.organization.hard_delete()

        assert Organization.objects.filter(pk=other_tenancy.organization.pk).exists()
        assert Workspace.objects.filter(pk=other_tenancy.workspace.pk).exists()


@pytest.mark.django_db
class TestDeletionWorkflow:
    def test_a_pending_deletion_is_visible(self, tenancy):
        from django.utils import timezone

        assert tenancy.organization.is_deletion_pending is False

        tenancy.organization.deletion_requested_at = timezone.now()

        assert tenancy.organization.is_deletion_pending is True


@pytest.mark.django_db
class TestOrgSettingsViews:
    def test_an_owner_can_rename_the_organization(self, tenancy, client_for):
        client_for(tenancy.owner).post("/organization/settings/update/", {"name": "Renamed"})

        tenancy.organization.refresh_from_db()
        assert tenancy.organization.name == "Renamed"

    def test_archiving_and_restoring_round_trips(self, tenancy, client_for):
        client = client_for(tenancy.owner)
        url = f"/organization/workspaces/{tenancy.workspace.pk}/archived/"

        client.post(url, {"archived": "1"})
        tenancy.workspace.refresh_from_db()
        assert tenancy.workspace.is_archived is True

        client.post(url, {"archived": "0"})
        tenancy.workspace.refresh_from_db()
        assert tenancy.workspace.is_archived is False

    def test_another_orgs_workspace_cannot_be_archived(self, tenancy, other_tenancy, client_for):
        response = client_for(other_tenancy.owner).post(
            f"/organization/workspaces/{tenancy.workspace.pk}/archived/", {"archived": "1"}
        )

        assert response.status_code == 404
        tenancy.workspace.refresh_from_db()
        assert tenancy.workspace.is_archived is False

    def test_creating_a_workspace_makes_the_creator_its_admin(self, tenancy, client_for):
        client_for(tenancy.owner).post("/organization/workspaces/create/", {"name": "Fresh"})

        workspace = Workspace.objects.for_org(tenancy.organization.pk).get(name="Fresh")
        assert WorkspaceMembership.objects.get(user=tenancy.owner, workspace=workspace).workspace_role == "admin"

    def test_duplicate_names_are_refused(self, tenancy, client_for):
        client_for(tenancy.owner).post("/organization/workspaces/create/", {"name": tenancy.workspace.name})

        assert Workspace.objects.for_org(tenancy.organization.pk).filter(name=tenancy.workspace.name).count() == 1
