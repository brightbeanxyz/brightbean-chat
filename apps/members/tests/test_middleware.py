"""RBACMiddleware: context resolution, 404s and the archived-workspace fix."""

import pytest

from apps.members.models import WorkspaceMembership
from apps.workspaces.models import Workspace


@pytest.mark.django_db
class TestWorkspaceResolution:
    def test_a_member_gets_workspace_context(self, tenancy, client_for):
        client = client_for(tenancy.user_for("editor"))

        response = client.get(f"/w/{tenancy.workspace.pk}/")

        assert response.status_code == 200
        assert response.wsgi_request.workspace == tenancy.workspace
        assert response.wsgi_request.workspace_membership.workspace_role == "editor"
        assert response.wsgi_request.org == tenancy.organization

    def test_a_non_member_gets_404_not_403(self, tenancy, other_tenancy, client_for):
        """SECURITY-BASELINE §1. Studio raises PermissionDenied here, which
        confirms the id names a real workspace."""
        client = client_for(other_tenancy.owner)

        assert client.get(f"/w/{tenancy.workspace.pk}/").status_code == 404

    def test_a_nonexistent_workspace_is_indistinguishable(self, other_tenancy, client_for):
        import uuid

        client = client_for(other_tenancy.owner)

        assert client.get(f"/w/{uuid.uuid4()}/").status_code == 404

    def test_context_is_none_for_anonymous_requests(self, client):
        response = client.get("/accounts/login/")

        assert response.wsgi_request.org is None
        assert response.wsgi_request.workspace is None

    def test_an_anonymous_request_to_a_workspace_url_redirects_to_login(self, tenancy, client):
        """404 here would break every 'sign in and come back' link."""
        response = client.get(f"/w/{tenancy.workspace.pk}/")

        assert response.status_code == 302
        assert "/accounts/login/" in response.headers["Location"]


@pytest.mark.django_db
class TestArchivedWorkspaces:
    """Studio filters is_archived on the last_workspace_id fallback but not on
    the URL path, so an archived workspace stays fully usable to anyone with a
    direct link. Both paths filter it here."""

    def test_the_url_path_404s_an_archived_workspace(self, tenancy, client_for):
        tenancy.workspace.is_archived = True
        tenancy.workspace.save(update_fields=["is_archived"])

        assert client_for(tenancy.owner).get(f"/w/{tenancy.workspace.pk}/").status_code == 404

    def test_the_fallback_ignores_an_archived_workspace(self, tenancy, client_for):
        tenancy.workspace.is_archived = True
        tenancy.workspace.save(update_fields=["is_archived"])

        response = client_for(tenancy.owner).get("/organization/settings/")

        assert response.wsgi_request.workspace is None

    def test_an_archived_workspace_is_still_visible_at_org_level(self, tenancy, client_for):
        """Otherwise there would be no way to restore it."""
        tenancy.workspace.is_archived = True
        tenancy.workspace.save(update_fields=["is_archived"])

        response = client_for(tenancy.owner).get("/organization/workspaces/")

        assert response.status_code == 200
        assert tenancy.workspace.name.encode() in response.content


@pytest.mark.django_db
class TestLastWorkspaceTracking:
    def test_visiting_a_workspace_records_it(self, tenancy, client_for):
        second = Workspace.objects.create(organization=tenancy.organization, name="second")
        WorkspaceMembership.objects.create(user=tenancy.owner, workspace=second, workspace_role="admin")

        client_for(tenancy.owner).get(f"/w/{second.pk}/")

        tenancy.owner.refresh_from_db()
        assert tenancy.owner.last_workspace_id == second.pk

    def test_a_stale_last_workspace_id_is_ignored(self, tenancy, client_for):
        """It is a bare UUID with no foreign key, so nothing else would."""
        import uuid

        tenancy.owner.last_workspace_id = uuid.uuid4()
        tenancy.owner.save(update_fields=["last_workspace_id"])

        response = client_for(tenancy.owner).get("/organization/settings/")

        assert response.status_code == 200
        assert response.wsgi_request.workspace is None


class TestDocumentedAssumptions:
    def test_the_single_org_assumption_is_written_down(self):
        """The brief says to keep .first() but document it."""
        from apps.members import middleware

        doc = middleware.__doc__ or ""
        assert "one organization per user" in doc

    def test_the_kwarg_name_is_the_contract(self):
        from apps.members.middleware import WORKSPACE_URL_KWARG

        assert WORKSPACE_URL_KWARG == "workspace_id"
