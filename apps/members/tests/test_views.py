"""Role enforcement over HTTP — the tests the brief asks for by name."""

import pytest

from apps.members.models import OrgMembership, WorkspaceMembership
from apps.members.roles import OrgRole
from tests.support import create_user

MUTATING_WORKSPACE_ROUTES = [
    "/w/{workspace}/settings/update/",
    "/w/{workspace}/settings/credentials/instagram/",
    "/w/{workspace}/settings/credentials/instagram/clear/",
]


@pytest.mark.django_db
class TestViewerIsReadOnly:
    """SPEC §4: Viewer is read-only everywhere."""

    @pytest.mark.parametrize("route", MUTATING_WORKSPACE_ROUTES)
    def test_viewer_cannot_post_to_workspace_routes(self, tenancy, client_for, route):
        client = client_for(tenancy.user_for("viewer"))

        response = client.post(route.format(workspace=tenancy.workspace.pk))

        assert response.status_code == 403

    def test_viewer_cannot_reach_workspace_settings_at_all(self, tenancy, client_for):
        client = client_for(tenancy.user_for("viewer"))

        assert client.get(f"/w/{tenancy.workspace.pk}/settings/").status_code == 403

    def test_viewer_can_still_read_the_dashboard(self, tenancy, client_for):
        client = client_for(tenancy.user_for("viewer"))

        assert client.get(f"/w/{tenancy.workspace.pk}/").status_code == 200

    def test_viewer_may_still_switch_workspace(self, tenancy, client_for):
        """Which workspace you are looking at is a personal preference, not
        workspace data — the one POST a Viewer legitimately makes."""
        client = client_for(tenancy.user_for("viewer"))

        response = client.post(f"/w/{tenancy.workspace.pk}/switch/")

        assert response.status_code == 302


@pytest.mark.django_db
class TestEditorCannotManageMembers:
    def test_editor_is_refused_workspace_settings(self, tenancy, client_for):
        client = client_for(tenancy.user_for("editor"))

        assert client.get(f"/w/{tenancy.workspace.pk}/settings/").status_code == 403

    def test_editor_cannot_invite(self, tenancy, client_for):
        """Editor is an org member, so the org-level gate refuses first."""
        client = client_for(tenancy.user_for("editor"))

        response = client.post("/organization/members/invite/", {"email": "x@example.test", "org_role": "member"})

        assert response.status_code == 403

    def test_editor_has_no_workspace_to_assign_roles_in(self, tenancy, client_for):
        """manage_workspaces is open to org members, but the form only offers
        workspaces where the caller holds manage_members — Editor holds none."""
        client = client_for(tenancy.user_for("editor"))
        membership = OrgMembership.objects.get(user=tenancy.user_for("agent"))

        response = client.get(f"/organization/members/{membership.pk}/workspaces/")

        assert response.status_code == 200
        assert b"cannot manage members in any workspace" in response.content

    def test_editor_cannot_change_workspace_assignments(self, tenancy, client_for):
        client = client_for(tenancy.user_for("editor"))
        membership = OrgMembership.objects.get(user=tenancy.user_for("agent"))

        client.post(
            f"/organization/members/{membership.pk}/workspaces/",
            {f"ws_{tenancy.workspace.pk}": "1", f"ws_role_{tenancy.workspace.pk}": "admin"},
        )

        assert (
            WorkspaceMembership.objects.get(user=tenancy.user_for("agent"), workspace=tenancy.workspace).workspace_role
            == "agent"
        )


@pytest.mark.django_db
class TestWorkspaceAdminCanManageMembers:
    def test_a_workspace_admin_may_assign_roles_there(self, tenancy, client_for):
        """A workspace Admin who is only an org member still manages their own
        workspace's people — this is where manage_members binds."""
        client = client_for(tenancy.user_for("admin"))
        membership = OrgMembership.objects.get(user=tenancy.user_for("agent"))

        client.post(
            f"/organization/members/{membership.pk}/workspaces/",
            {f"ws_{tenancy.workspace.pk}": "1", f"ws_role_{tenancy.workspace.pk}": "editor"},
        )

        assert (
            WorkspaceMembership.objects.get(user=tenancy.user_for("agent"), workspace=tenancy.workspace).workspace_role
            == "editor"
        )


@pytest.mark.django_db
class TestOrgLevelGates:
    def test_a_member_can_read_org_settings(self, tenancy, client_for):
        assert client_for(tenancy.user_for("viewer")).get("/organization/settings/").status_code == 200

    def test_a_member_cannot_change_org_settings(self, tenancy, client_for):
        response = client_for(tenancy.user_for("viewer")).post("/organization/settings/update/", {"name": "Hijack"})

        assert response.status_code == 403
        tenancy.organization.refresh_from_db()
        assert tenancy.organization.name != "Hijack"

    def test_a_member_cannot_create_workspaces(self, tenancy, client_for):
        response = client_for(tenancy.user_for("editor")).post("/organization/workspaces/create/", {"name": "Sneaky"})

        assert response.status_code == 403

    def test_an_owner_can(self, tenancy, client_for):
        response = client_for(tenancy.owner).post("/organization/workspaces/create/", {"name": "Second"})

        assert response.status_code == 302
        assert tenancy.organization.workspaces.filter(name="Second").exists()

    def test_a_user_with_no_organization_is_refused(self, client_for):
        stranger = create_user("nobody@example.test")

        assert client_for(stranger).get("/organization/settings/").status_code == 403


@pytest.mark.django_db
class TestInviteFlowOverHttp:
    def test_an_owner_can_invite_and_the_invitation_appears(self, tenancy, client_for):
        client = client_for(tenancy.owner)

        client.post(
            "/organization/members/invite/",
            {
                "email": "newcomer@example.test",
                "org_role": OrgRole.MEMBER.value,
                f"ws_{tenancy.workspace.pk}": "1",
                f"ws_role_{tenancy.workspace.pk}": "agent",
            },
        )

        response = client.get("/organization/members/")
        assert b"newcomer@example.test" in response.content

    def test_an_unknown_invite_token_renders_the_same_page_as_an_expired_one(self, client):
        response = client.get("/invite/not-a-real-token/")

        assert response.status_code == 404
        assert b"no longer valid" in response.content
