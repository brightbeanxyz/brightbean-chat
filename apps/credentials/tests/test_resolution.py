"""The SPEC §4 resolution chain: workspace override → organization → env.

Studio's ``resolve_platform_credentials`` runs the other way (env dominant with
an org fallback), which makes the most specific configuration the least
authoritative. Deviation 4 inverts it.
"""

import pytest

from apps.credentials.models import PlatformCredential, WorkspaceCredentialOverride
from apps.credentials.resolution import (
    SOURCE_ENV,
    SOURCE_NONE,
    SOURCE_ORGANIZATION,
    SOURCE_WORKSPACE,
    resolve_platform_credentials,
)

PLATFORM = "instagram"
ENV_SET = {"client_id": "env-id", "client_secret": "env-secret"}
ORG_SET = {"client_id": "org-id", "client_secret": "org-secret"}
WS_SET = {"client_id": "ws-id", "client_secret": "ws-secret"}


@pytest.fixture
def env_credentials(settings):
    settings.PLATFORM_CREDENTIALS_FROM_ENV = {PLATFORM: dict(ENV_SET)}
    return ENV_SET


def _org(tenancy, credentials):
    return PlatformCredential.objects.create(
        organization=tenancy.organization, platform=PLATFORM, credentials=credentials
    )


def _workspace(tenancy, credentials):
    return WorkspaceCredentialOverride.objects.create(
        workspace=tenancy.workspace, platform=PLATFORM, credentials=credentials
    )


@pytest.mark.django_db
class TestTheThreeLevels:
    def test_nothing_configured_anywhere(self, tenancy):
        resolution = resolve_platform_credentials(PLATFORM, workspace=tenancy.workspace)

        assert resolution.source == SOURCE_NONE
        assert resolution.credentials == {}
        assert not resolution

    def test_env_only(self, tenancy, env_credentials):
        resolution = resolve_platform_credentials(PLATFORM, workspace=tenancy.workspace)

        assert resolution.source == SOURCE_ENV
        assert resolution.credentials == ENV_SET

    def test_the_organization_beats_env(self, tenancy, env_credentials):
        _org(tenancy, ORG_SET)

        resolution = resolve_platform_credentials(PLATFORM, workspace=tenancy.workspace)

        assert resolution.source == SOURCE_ORGANIZATION
        assert resolution.credentials == ORG_SET

    def test_the_workspace_beats_both(self, tenancy, env_credentials):
        _org(tenancy, ORG_SET)
        _workspace(tenancy, WS_SET)

        resolution = resolve_platform_credentials(PLATFORM, workspace=tenancy.workspace)

        assert resolution.source == SOURCE_WORKSPACE
        assert resolution.credentials == WS_SET

    def test_removing_a_level_falls_back_to_the_next(self, tenancy, env_credentials):
        _org(tenancy, ORG_SET)
        override = _workspace(tenancy, WS_SET)

        override.delete()
        assert resolve_platform_credentials(PLATFORM, workspace=tenancy.workspace).source == SOURCE_ORGANIZATION

        PlatformCredential.objects.for_org(tenancy.organization.pk).delete()
        assert resolve_platform_credentials(PLATFORM, workspace=tenancy.workspace).source == SOURCE_ENV

    def test_organization_only_resolution_ignores_workspace_overrides(self, tenancy):
        _org(tenancy, ORG_SET)
        _workspace(tenancy, WS_SET)

        resolution = resolve_platform_credentials(PLATFORM, organization=tenancy.organization)

        assert resolution.source == SOURCE_ORGANIZATION

    def test_the_organization_is_inferred_from_the_workspace(self, tenancy):
        _org(tenancy, ORG_SET)

        assert resolve_platform_credentials(PLATFORM, workspace=tenancy.workspace).source == SOURCE_ORGANIZATION


@pytest.mark.django_db
class TestIncompleteLevelsFallThrough:
    """A level wins only if it satisfies REQUIRED_CREDENTIAL_KEYS.

    The alternatives were both worse: merging keys across levels assembles
    credential sets no provider will accept, and letting an incomplete override
    win means one blank field silently disables a working organization.
    """

    def test_an_incomplete_workspace_override_is_skipped(self, tenancy, env_credentials):
        _org(tenancy, ORG_SET)
        _workspace(tenancy, {"client_id": "ws-id"})

        resolution = resolve_platform_credentials(PLATFORM, workspace=tenancy.workspace)

        assert resolution.source == SOURCE_ORGANIZATION
        assert resolution.credentials == ORG_SET

    def test_an_incomplete_organization_is_skipped(self, tenancy, env_credentials):
        _org(tenancy, {"client_secret": "org-secret"})

        resolution = resolve_platform_credentials(PLATFORM, workspace=tenancy.workspace)

        assert resolution.source == SOURCE_ENV

    def test_incomplete_env_resolves_to_nothing(self, tenancy, settings):
        settings.PLATFORM_CREDENTIALS_FROM_ENV = {PLATFORM: {"client_id": "env-id"}}

        assert resolve_platform_credentials(PLATFORM, workspace=tenancy.workspace).source == SOURCE_NONE

    def test_every_level_incomplete_resolves_to_nothing(self, tenancy, settings):
        settings.PLATFORM_CREDENTIALS_FROM_ENV = {PLATFORM: {"client_id": "env-id"}}
        _org(tenancy, {"client_id": "org-id"})
        _workspace(tenancy, {"client_id": "ws-id"})

        assert resolve_platform_credentials(PLATFORM, workspace=tenancy.workspace).source == SOURCE_NONE

    def test_keys_are_never_merged_across_levels(self, tenancy):
        _org(tenancy, ORG_SET)
        _workspace(tenancy, {"client_id": "ws-id"})

        resolution = resolve_platform_credentials(PLATFORM, workspace=tenancy.workspace)

        assert resolution.credentials["client_id"] == "org-id"

    def test_the_skip_is_logged_without_the_values(self, tenancy, caplog, env_credentials):
        import logging

        _workspace(tenancy, {"client_id": "ws-id"})

        with caplog.at_level(logging.DEBUG, logger="apps.credentials.resolution"):
            resolve_platform_credentials(PLATFORM, workspace=tenancy.workspace)

        text = caplog.text
        assert "client_secret/app_secret" in text
        assert "ws-id" not in text


@pytest.mark.django_db
class TestTenantIsolation:
    def test_one_workspaces_override_does_not_leak_into_another(self, tenancy, other_tenancy):
        _workspace(tenancy, WS_SET)

        assert resolve_platform_credentials(PLATFORM, workspace=other_tenancy.workspace).source == SOURCE_NONE

    def test_one_orgs_credentials_do_not_leak_into_another(self, tenancy, other_tenancy):
        _org(tenancy, ORG_SET)

        assert resolve_platform_credentials(PLATFORM, organization=other_tenancy.organization).source == SOURCE_NONE


class TestEnvVarScanning:
    def test_platform_env_vars_are_grouped(self, monkeypatch):
        import importlib

        monkeypatch.setenv("PLATFORM_INSTAGRAM_CLIENT_ID", "abc")
        monkeypatch.setenv("PLATFORM_INSTAGRAM_CLIENT_SECRET", "def")
        monkeypatch.setenv("PLATFORM_WHATSAPP_APP_ID", "ghi")

        base = importlib.import_module("config.settings.base")
        collected = base._platform_credentials_from_env()

        assert collected["instagram"] == {"client_id": "abc", "client_secret": "def"}
        assert collected["whatsapp"] == {"app_id": "ghi"}

    def test_blank_values_are_ignored(self, monkeypatch):
        import importlib

        monkeypatch.setenv("PLATFORM_TELEGRAM_TOKEN", "   ")

        base = importlib.import_module("config.settings.base")

        assert "telegram" not in base._platform_credentials_from_env()

    def test_the_settings_slug_list_matches_the_enum(self):
        """A platform missing from the tuple silently ignores its env vars."""
        from apps.common.checks import check_platform_env_slugs

        assert check_platform_env_slugs() == []
