"""Regressions for the defects the review of this branch turned up."""

import pytest

from apps.members.models import OrgMembership, WorkspaceMembership
from apps.members.roles import OrgRole, WorkspaceRole
from apps.members.services import (
    MembershipError,
    manageable_workspaces,
    remove_member,
    update_member_org_role,
    update_workspace_assignments,
    workspace_authority_map,
)
from apps.workspaces.models import Workspace
from tests.support import create_user


@pytest.fixture
def partial_admin(tenancy):
    """An org admin who manages workspace A but not workspace B."""
    caller = create_user("partial.admin@acme.test")
    OrgMembership.objects.create(user=caller, organization=tenancy.organization, org_role=OrgRole.ADMIN)
    WorkspaceMembership.objects.create(user=caller, workspace=tenancy.workspace, workspace_role=WorkspaceRole.ADMIN)
    return caller


@pytest.mark.django_db
class TestPartialAuthorityCanStillSave:
    """The form renders only the workspaces the caller manages, so the service
    must treat exactly that set as the one a submission speaks for. Scoping it
    to the whole org made the page a permanent dead end."""

    def test_a_membership_outside_the_callers_scope_is_left_alone(self, tenancy, partial_admin):
        other = Workspace.objects.create(organization=tenancy.organization, name="B")
        target = tenancy.user_for("agent")
        WorkspaceMembership.objects.create(user=target, workspace=other, workspace_role=WorkspaceRole.VIEWER)

        update_workspace_assignments(
            tenancy.organization,
            target,
            [{"workspace_id": str(tenancy.workspace.pk), "role": WorkspaceRole.EDITOR.value}],
            inviter=partial_admin,
        )

        assert (
            WorkspaceMembership.objects.get(user=target, workspace=tenancy.workspace).workspace_role
            == WorkspaceRole.EDITOR
        )
        # Untouched, because it was never on the form.
        assert WorkspaceMembership.objects.filter(user=target, workspace=other).exists()

    def test_omission_still_removes_within_the_scope(self, tenancy, partial_admin):
        target = tenancy.user_for("agent")

        update_workspace_assignments(tenancy.organization, target, [], inviter=partial_admin)

        assert not WorkspaceMembership.objects.filter(user=target, workspace=tenancy.workspace).exists()

    def test_granting_into_an_unmanaged_workspace_is_still_refused(self, tenancy, partial_admin):
        other = Workspace.objects.create(organization=tenancy.organization, name="B")

        with pytest.raises(MembershipError, match="cannot manage members in that workspace"):
            update_workspace_assignments(
                tenancy.organization,
                tenancy.user_for("agent"),
                [{"workspace_id": str(other.pk), "role": WorkspaceRole.VIEWER.value}],
                inviter=partial_admin,
            )

    def test_the_form_and_the_service_agree_on_the_scope(self, tenancy, partial_admin):
        Workspace.objects.create(organization=tenancy.organization, name="B")

        rendered = {str(w.pk) for w in manageable_workspaces(partial_admin, tenancy.organization)}

        assert rendered == set(workspace_authority_map(partial_admin, tenancy.organization))

    def test_the_page_saves_over_http(self, tenancy, partial_admin, client_for):
        other = Workspace.objects.create(organization=tenancy.organization, name="B")
        target = tenancy.user_for("agent")
        WorkspaceMembership.objects.create(user=target, workspace=other, workspace_role=WorkspaceRole.VIEWER)
        membership = OrgMembership.objects.get(user=target, organization=tenancy.organization)

        response = client_for(partial_admin).post(
            f"/organization/members/{membership.pk}/workspaces/",
            {f"ws_{tenancy.workspace.pk}": "1", f"ws_role_{tenancy.workspace.pk}": "editor"},
            follow=True,
        )

        assert b"Workspace access updated" in response.content


@pytest.mark.django_db
class TestAdminTierIsSymmetric:
    """Only an owner may create an admin, so only an owner may unmake one.
    Otherwise the privilege is destroyable but not restorable."""

    @pytest.fixture
    def two_admins(self, tenancy):
        first = create_user("admin.one@acme.test")
        second = create_user("admin.two@acme.test")
        OrgMembership.objects.create(user=first, organization=tenancy.organization, org_role=OrgRole.ADMIN)
        membership = OrgMembership.objects.create(
            user=second, organization=tenancy.organization, org_role=OrgRole.ADMIN
        )
        return first, membership

    def test_an_admin_cannot_demote_a_peer_admin(self, tenancy, two_admins):
        caller, target = two_admins

        with pytest.raises(MembershipError, match="Only organization owners can change"):
            update_member_org_role(tenancy.organization, target, OrgRole.MEMBER, caller=caller)

    def test_an_admin_cannot_remove_a_peer_admin(self, tenancy, two_admins):
        caller, target = two_admins

        with pytest.raises(MembershipError, match="Only organization owners can remove"):
            remove_member(tenancy.organization, target, caller)

    def test_an_owner_can_do_both(self, tenancy, two_admins):
        _, target = two_admins

        update_member_org_role(tenancy.organization, target, OrgRole.MEMBER, caller=tenancy.owner)

        assert OrgMembership.objects.get(pk=target.pk).org_role == OrgRole.MEMBER

    def test_an_admin_can_still_manage_ordinary_members(self, tenancy, two_admins):
        caller, _ = two_admins
        member = OrgMembership.objects.get(user=tenancy.user_for("editor"))

        remove_member(tenancy.organization, member, caller)

        assert not OrgMembership.objects.filter(pk=member.pk).exists()

    def test_the_member_list_hides_controls_it_would_refuse(self, tenancy, two_admins, client_for):
        caller, target = two_admins

        content = client_for(caller).get("/organization/members/").content

        # The peer admin's row offers no role form and no remove button.
        assert f"members/{target.pk}/role/".encode() not in content
        assert f"members/{target.pk}/remove/".encode() not in content

    def test_the_owner_row_offers_no_controls_to_an_admin(self, tenancy, two_admins, client_for):
        caller, _ = two_admins
        owner_membership = tenancy.org_membership

        content = client_for(caller).get("/organization/members/").content

        assert f"members/{owner_membership.pk}/role/".encode() not in content


def count_queries(client, url: str) -> int:
    """Queries for one request, with the session already warm.

    The first request of a logged-in client pays for session and auth lookups
    that say nothing about the view.
    """
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    client.get(url)
    with CaptureQueriesContext(connection) as captured:
        client.get(url)
    return len(captured)


@pytest.mark.django_db
class TestAuthorityLookupIsBounded:
    def test_the_member_list_does_not_scale_with_workspace_count(self, tenancy, client_for):
        """workspace_authority_level used to cost two queries per workspace, on
        a page every org member can open."""
        client = client_for(tenancy.owner)
        before = count_queries(client, "/organization/members/")

        for index in range(12):
            Workspace.objects.create(organization=tenancy.organization, name=f"extra-{index}")

        assert count_queries(client, "/organization/members/") == before
