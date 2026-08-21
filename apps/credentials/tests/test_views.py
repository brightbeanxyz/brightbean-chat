"""The workspace credential-override UI (Admin only)."""

import json

import pytest

from apps.credentials.models import PlatformCredential, WorkspaceCredentialOverride

COMPLETE = {"client_id": "id-12345", "client_secret": "secret-67890"}


def _url(tenancy, suffix=""):
    return f"/w/{tenancy.workspace.pk}/settings/credentials/{suffix}"


@pytest.mark.django_db
class TestAccessControl:
    @pytest.mark.parametrize("role", ["editor", "agent", "viewer"])
    def test_only_admins_may_look(self, tenancy, client_for, role):
        assert client_for(tenancy.user_for(role)).get(_url(tenancy)).status_code == 403

    def test_an_admin_may(self, tenancy, client_for):
        assert client_for(tenancy.user_for("admin")).get(_url(tenancy)).status_code == 200


@pytest.mark.django_db
class TestSettingAnOverride:
    def test_it_stores_encrypted_values(self, tenancy, client_for):
        from django.db import connection

        client = client_for(tenancy.user_for("admin"))

        client.post(_url(tenancy, "instagram/"), {"credentials": json.dumps(COMPLETE)})

        override = WorkspaceCredentialOverride.objects.for_workspace(tenancy.workspace).get(platform="instagram")
        assert override.credentials == COMPLETE
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT credentials FROM credentials_workspace_credential_override WHERE id = %s", [str(override.pk)]
            )
            assert "secret-67890" not in cursor.fetchone()[0]

    def test_the_page_never_renders_the_stored_secret(self, tenancy, client_for):
        WorkspaceCredentialOverride.objects.create(
            workspace=tenancy.workspace, platform="instagram", credentials=COMPLETE
        )
        client = client_for(tenancy.user_for("admin"))

        listing = client.get(_url(tenancy))
        editing = client.get(_url(tenancy, "instagram/"))

        assert b"secret-67890" not in listing.content
        assert b"secret-67890" not in editing.content
        assert b"****7890" in listing.content

    def test_the_listing_names_the_level_in_force(self, tenancy, client_for, settings):
        settings.PLATFORM_CREDENTIALS_FROM_ENV = {"instagram": dict(COMPLETE)}
        client = client_for(tenancy.user_for("admin"))

        assert b"Deployment environment" in client.get(_url(tenancy)).content

        PlatformCredential.objects.create(organization=tenancy.organization, platform="instagram", credentials=COMPLETE)
        assert b"Organization" in client.get(_url(tenancy)).content

        WorkspaceCredentialOverride.objects.create(
            workspace=tenancy.workspace, platform="instagram", credentials=COMPLETE
        )
        assert b"This workspace" in client.get(_url(tenancy)).content

    def test_an_incomplete_override_is_flagged(self, tenancy, client_for):
        WorkspaceCredentialOverride.objects.create(
            workspace=tenancy.workspace, platform="instagram", credentials={"client_id": "a"}
        )

        response = client_for(tenancy.user_for("admin")).get(_url(tenancy))

        assert b"Incomplete" in response.content

    def test_clearing_removes_the_row(self, tenancy, client_for):
        WorkspaceCredentialOverride.objects.create(
            workspace=tenancy.workspace, platform="instagram", credentials=COMPLETE
        )
        client = client_for(tenancy.user_for("admin"))

        client.post(_url(tenancy, "instagram/clear/"))

        assert not WorkspaceCredentialOverride.objects.for_workspace(tenancy.workspace).exists()

    def test_an_unknown_platform_is_a_404(self, tenancy, client_for):
        assert client_for(tenancy.user_for("admin")).get(_url(tenancy, "myspace/")).status_code == 404


@pytest.mark.django_db
class TestCredentialsNeverReachTheLogs:
    """SECURITY-BASELINE §5, with the global scrubber from Layer 0."""

    def test_a_failed_resolution_logs_key_names_only(self, tenancy, caplog):
        import logging

        from apps.credentials.resolution import resolve_platform_credentials

        WorkspaceCredentialOverride.objects.create(
            workspace=tenancy.workspace, platform="instagram", credentials={"client_id": "id-12345"}
        )

        with caplog.at_level(logging.DEBUG):
            resolve_platform_credentials("instagram", workspace=tenancy.workspace)

        assert "id-12345" not in caplog.text
