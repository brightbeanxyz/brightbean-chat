"""The org-level API keys page.

Two gates at two tiers, and the second is the one the issue's acceptance
criteria call out: *"an org member without ``manage_api_keys`` cannot reach the
key UI even if they are a workspace admin"*. Under the decision this issue took,
that becomes "org **admin** or owner reaches the page; issuing into a workspace
additionally needs ``manage_api_keys`` there".
"""

import pytest

from apps.api.models import ApiKey
from apps.api.services import ApiKeysError, issue_api_key
from apps.members.models import WorkspaceMembership
from apps.members.roles import WorkspaceRole

LIST = "/organization/api-keys/"
ISSUE = "/organization/api-keys/issue/"


@pytest.mark.django_db
class TestPageAccess:
    def test_an_org_owner_can_open_it(self, client_for, tenancy):
        assert client_for(tenancy.owner).get(LIST).status_code == 200

    def test_an_ordinary_org_member_cannot_even_as_a_workspace_admin(self, client_for, tenancy):
        """The acceptance criterion, spelled out.

        ``tenancy.members["admin"]`` is a *workspace* admin and an ordinary org
        member — the exact person the criterion names.
        """
        admin = tenancy.user_for(WorkspaceRole.ADMIN)
        assert WorkspaceMembership.objects.get(user=admin, workspace=tenancy.workspace).effective_permissions[
            "manage_api_keys"
        ]

        assert client_for(admin).get(LIST).status_code == 403

    def test_an_anonymous_visitor_is_sent_to_log_in(self, client):
        response = client.get(LIST)

        assert response.status_code == 302
        assert "/accounts/login/" in response["Location"]

    def test_a_user_with_no_organization_is_refused(self, client_for, user):
        assert client_for(user).get(LIST).status_code == 403


@pytest.mark.django_db
class TestIssuance:
    def test_it_shows_the_key_once_and_stores_only_a_digest(self, client_for, tenancy):
        response = client_for(tenancy.owner).post(
            ISSUE, {"name": "Zapier", "workspace": str(tenancy.workspace.pk), "scopes": ["read"]}
        )

        assert response.status_code == 200
        body = response.content.decode()
        api_key = ApiKey.objects.for_workspace(tenancy.workspace).get()
        assert api_key.name == "Zapier"
        assert api_key.scopes == ["read"]
        assert "bb_" in body

        # And the list page never renders it again.
        listed = client_for(tenancy.owner).get(LIST).content.decode()
        assert api_key.display_handle in listed
        token = [word for word in body.split() if word.startswith("bb_")][0].strip("<>\"'")
        assert token not in listed

    def test_it_is_a_direct_response_not_a_redirect(self, client_for, tenancy):
        """No redirect, so the plaintext never touches the session.

        The messages framework stores in ``django_session``, which in this
        project is a database table — a redirect-then-show would park a live
        credential there for the life of the session (SECURITY-BASELINE §5).
        """
        response = client_for(tenancy.owner).post(
            ISSUE, {"name": "Zapier", "workspace": str(tenancy.workspace.pk), "scopes": ["read"]}
        )

        assert response.status_code == 200
        assert "Location" not in response

    def test_a_workspace_outside_the_org_is_refused_without_saying_why(self, client_for, tenancy, other_tenancy):
        response = client_for(tenancy.owner).post(
            ISSUE, {"name": "Sneaky", "workspace": str(other_tenancy.workspace.pk), "scopes": ["read"]}
        )

        assert response.status_code == 400
        assert not ApiKey.objects.for_workspace(other_tenancy.workspace).exists()
        # The same copy a missing selection gets: a different message would
        # confirm the id names a real workspace somewhere else.
        assert "Choose a workspace." in response.content.decode()

    def test_a_key_needs_a_name_and_a_scope(self, client_for, tenancy):
        blank_name = client_for(tenancy.owner).post(
            ISSUE, {"name": "  ", "workspace": str(tenancy.workspace.pk), "scopes": ["read"]}
        )
        no_scope = client_for(tenancy.owner).post(ISSUE, {"name": "Nameless", "workspace": str(tenancy.workspace.pk)})

        assert blank_name.status_code == 400
        assert no_scope.status_code == 400
        assert not ApiKey.objects.for_workspace(tenancy.workspace).exists()

    def test_revoking_takes_effect_and_is_idempotent(self, client_for, tenancy):
        api_key = issue_api_key(workspace=tenancy.workspace, issuer=tenancy.owner, name="Zapier", scopes=["read"])
        url = f"/organization/api-keys/{api_key.pk}/revoke/"

        assert client_for(tenancy.owner).post(url).status_code == 302
        api_key.refresh_from_db()
        assert api_key.revoked_at is not None

        first = api_key.revoked_at
        client_for(tenancy.owner).post(url)
        api_key.refresh_from_db()
        assert api_key.revoked_at == first

    def test_revoking_another_orgs_key_is_a_404(self, client_for, tenancy, other_tenancy):
        theirs = issue_api_key(
            workspace=other_tenancy.workspace, issuer=other_tenancy.owner, name="Theirs", scopes=["read"]
        )

        response = client_for(tenancy.owner).post(f"/organization/api-keys/{theirs.pk}/revoke/")

        assert response.status_code == 404
        theirs.refresh_from_db()
        assert theirs.revoked_at is None


@pytest.mark.django_db
class TestIssuanceService:
    """The rules, re-checked where a shell or a management command would hit them."""

    def test_an_org_member_cannot_issue_even_as_a_workspace_admin(self, tenancy):
        admin = tenancy.user_for(WorkspaceRole.ADMIN)

        with pytest.raises(ApiKeysError, match="organization owner or admin"):
            issue_api_key(workspace=tenancy.workspace, issuer=admin, name="No", scopes=["read"])

    def test_an_org_admin_without_the_workspace_permission_cannot_issue_into_it(self, tenancy):
        """The second gate: org authority does not reach inside a workspace.

        The owner is demoted to editor in the workspace, which is the role that
        holds everything except the admin-only keys — ``manage_api_keys`` among
        them.
        """
        membership = WorkspaceMembership.objects.get(user=tenancy.owner, workspace=tenancy.workspace)
        membership.workspace_role = WorkspaceRole.EDITOR
        membership.save(update_fields=["workspace_role"])

        with pytest.raises(ApiKeysError, match="manage_api_keys"):
            issue_api_key(workspace=tenancy.workspace, issuer=tenancy.owner, name="No", scopes=["read"])

    def test_an_issuer_cannot_grant_a_scope_they_do_not_hold(self, tenancy):
        """Scopes are capped once, at issuance, against the issuer's permissions."""
        membership = WorkspaceMembership.objects.get(user=tenancy.owner, workspace=tenancy.workspace)
        membership.workspace_role = WorkspaceRole.VIEWER
        membership.save(update_fields=["workspace_role"])

        with pytest.raises(ApiKeysError):
            issue_api_key(workspace=tenancy.workspace, issuer=tenancy.owner, name="No", scopes=["write"])

    def test_an_unknown_scope_is_refused(self, tenancy):
        with pytest.raises(ApiKeysError, match="Unknown scope"):
            issue_api_key(workspace=tenancy.workspace, issuer=tenancy.owner, name="No", scopes=["root"])

    def test_a_workspace_cannot_hoard_keys(self, tenancy, settings):
        """The cap is the only thing bounding how many live credentials exist."""
        from apps.api.services import MAX_KEYS_PER_WORKSPACE

        settings_cap = MAX_KEYS_PER_WORKSPACE
        for index in range(settings_cap):
            issue_api_key(workspace=tenancy.workspace, issuer=tenancy.owner, name=f"key {index}", scopes=["read"])

        with pytest.raises(ApiKeysError, match="active keys"):
            issue_api_key(workspace=tenancy.workspace, issuer=tenancy.owner, name="one too many", scopes=["read"])

    def test_a_revoked_key_frees_a_slot(self, tenancy):
        """The cap counts live keys, so revoking is the way back under it."""
        from apps.api.services import MAX_KEYS_PER_WORKSPACE, revoke_api_key

        keys = [
            issue_api_key(workspace=tenancy.workspace, issuer=tenancy.owner, name=f"key {index}", scopes=["read"])
            for index in range(MAX_KEYS_PER_WORKSPACE)
        ]
        revoke_api_key(keys[0])

        issue_api_key(workspace=tenancy.workspace, issuer=tenancy.owner, name="replacement", scopes=["read"])

    def test_the_plaintext_is_returned_once_and_never_persisted(self, tenancy):
        api_key = issue_api_key(workspace=tenancy.workspace, issuer=tenancy.owner, name="Zapier", scopes=["read"])

        assert api_key.raw_token.startswith("bb_")
        reloaded = ApiKey.objects.for_workspace(tenancy.workspace).get(pk=api_key.pk)
        assert reloaded.raw_token is None
