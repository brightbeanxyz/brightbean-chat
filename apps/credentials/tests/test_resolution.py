"""The resolution chain: deployment env → organization.

Environment-first, which is BrightBean Studio's direction and the inverse of
what this project shipped first. Platform app credentials are developer
credentials, so the deployment's own environment is the authoritative level and
the organization row is the fallback for a deployment serving several
organizations. The workspace-level override that used to sit on top is gone
along with its settings page.
"""

import logging

import pytest

from apps.credentials.models import PlatformCredential
from apps.credentials.resolution import (
    SOURCE_ENV,
    SOURCE_NONE,
    SOURCE_ORGANIZATION,
    resolve_platform_credentials,
)

PLATFORM = "instagram"
ENV_SET = {"client_id": "env-id", "client_secret": "env-secret"}
ORG_SET = {"client_id": "org-id", "client_secret": "org-secret"}


@pytest.fixture
def env_credentials(settings):
    settings.PLATFORM_CREDENTIALS_FROM_ENV = {PLATFORM: dict(ENV_SET)}
    return ENV_SET


@pytest.fixture
def no_env(settings):
    settings.PLATFORM_CREDENTIALS_FROM_ENV = {}


def _org(tenancy, credentials):
    return PlatformCredential.objects.create(
        organization=tenancy.organization, platform=PLATFORM, credentials=credentials
    )


@pytest.mark.django_db
class TestTheTwoLevels:
    def test_nothing_configured_anywhere(self, tenancy, no_env):
        resolution = resolve_platform_credentials(PLATFORM, workspace=tenancy.workspace)

        assert resolution.source == SOURCE_NONE
        assert resolution.credentials == {}
        assert not resolution

    def test_env_only(self, tenancy, env_credentials):
        resolution = resolve_platform_credentials(PLATFORM, workspace=tenancy.workspace)

        assert resolution.source == SOURCE_ENV
        assert resolution.credentials == ENV_SET

    def test_organization_only(self, tenancy, no_env):
        _org(tenancy, ORG_SET)

        resolution = resolve_platform_credentials(PLATFORM, workspace=tenancy.workspace)

        assert resolution.source == SOURCE_ORGANIZATION
        assert resolution.credentials == ORG_SET

    def test_env_beats_the_organization(self, tenancy, env_credentials):
        """The headline of this change, and the inverse of what shipped first.

        A self-hoster who sets an env var is not silently overridden by a row
        somebody entered in the admin.
        """
        _org(tenancy, ORG_SET)

        resolution = resolve_platform_credentials(PLATFORM, workspace=tenancy.workspace)

        assert resolution.source == SOURCE_ENV
        assert resolution.credentials == ENV_SET

    def test_removing_env_falls_back_to_the_organization(self, tenancy, settings, env_credentials):
        _org(tenancy, ORG_SET)

        settings.PLATFORM_CREDENTIALS_FROM_ENV = {}

        assert resolve_platform_credentials(PLATFORM, workspace=tenancy.workspace).source == SOURCE_ORGANIZATION

    def test_a_complete_env_level_issues_no_query(self, tenancy, env_credentials, django_assert_num_queries):
        """Env-first is also a query saved on every inbound delivery.

        ``meta_common.app_secret`` runs this per webhook, so the common
        self-hosted shape — one app in the environment — must not touch the
        credential table at all.
        """
        _org(tenancy, ORG_SET)

        with django_assert_num_queries(0):
            resolve_platform_credentials(PLATFORM, workspace=tenancy.workspace)

    def test_the_organization_is_inferred_from_the_workspace(self, tenancy, no_env):
        _org(tenancy, ORG_SET)

        assert resolve_platform_credentials(PLATFORM, workspace=tenancy.workspace).source == SOURCE_ORGANIZATION

    def test_an_explicit_organization_resolves_without_a_workspace(self, tenancy, no_env):
        _org(tenancy, ORG_SET)

        resolution = resolve_platform_credentials(PLATFORM, organization=tenancy.organization)

        assert resolution.source == SOURCE_ORGANIZATION


@pytest.mark.django_db
class TestIncompleteLevelsFallThrough:
    """A level wins only if it satisfies REQUIRED_CREDENTIAL_KEYS.

    The alternatives were both worse: merging keys across levels assembles
    credential sets no provider will accept, and letting an incomplete level win
    means one blank value silently disables a working configuration below it.
    """

    def test_an_incomplete_env_level_is_skipped(self, tenancy, settings):
        settings.PLATFORM_CREDENTIALS_FROM_ENV = {PLATFORM: {"client_id": "env-id"}}
        _org(tenancy, ORG_SET)

        resolution = resolve_platform_credentials(PLATFORM, workspace=tenancy.workspace)

        assert resolution.source == SOURCE_ORGANIZATION
        assert resolution.credentials == ORG_SET

    def test_an_incomplete_organization_is_skipped(self, tenancy, no_env):
        _org(tenancy, {"client_secret": "org-secret"})

        assert resolve_platform_credentials(PLATFORM, workspace=tenancy.workspace).source == SOURCE_NONE

    def test_incomplete_env_with_no_organization_resolves_to_nothing(self, tenancy, settings):
        settings.PLATFORM_CREDENTIALS_FROM_ENV = {PLATFORM: {"client_id": "env-id"}}

        assert resolve_platform_credentials(PLATFORM, workspace=tenancy.workspace).source == SOURCE_NONE

    def test_every_level_incomplete_resolves_to_nothing(self, tenancy, settings):
        settings.PLATFORM_CREDENTIALS_FROM_ENV = {PLATFORM: {"client_id": "env-id"}}
        _org(tenancy, {"client_id": "org-id"})

        assert resolve_platform_credentials(PLATFORM, workspace=tenancy.workspace).source == SOURCE_NONE

    def test_keys_are_never_merged_across_levels(self, tenancy, settings):
        """An env client_id must not be paired with an org client_secret."""
        settings.PLATFORM_CREDENTIALS_FROM_ENV = {PLATFORM: {"client_id": "env-id"}}
        _org(tenancy, ORG_SET)

        resolution = resolve_platform_credentials(PLATFORM, workspace=tenancy.workspace)

        assert resolution.credentials == ORG_SET

    def test_the_skip_is_logged_without_the_values(self, tenancy, settings, caplog):
        settings.PLATFORM_CREDENTIALS_FROM_ENV = {PLATFORM: {"client_id": "env-only-id"}}

        with caplog.at_level(logging.DEBUG, logger="apps.credentials.resolution"):
            resolve_platform_credentials(PLATFORM, workspace=tenancy.workspace)

        text = caplog.text
        assert "client_secret/app_secret" in text
        assert "env-only-id" not in text

    def test_a_verify_token_alone_is_not_reported_as_a_skip(self, tenancy, settings, caplog):
        """The shape a deployment using an organization row actually has.

        ``PLATFORM_<P>_VERIFY_TOKEN`` must be set in the environment even when
        the app id and secret come from the admin, so this env level is
        permanently "incomplete" — and it is resolved on every inbound
        delivery. Reporting it would be a log line per webhook for a correct
        configuration.
        """
        settings.PLATFORM_CREDENTIALS_FROM_ENV = {PLATFORM: {"verify_token": "hub-token"}}
        _org(tenancy, ORG_SET)

        with caplog.at_level(logging.DEBUG, logger="apps.credentials.resolution"):
            resolution = resolve_platform_credentials(PLATFORM, workspace=tenancy.workspace)

        assert resolution.source == SOURCE_ORGANIZATION
        assert "Skipping" not in caplog.text

    def test_nothing_configured_is_not_reported_as_a_skip(self, tenancy, no_env, caplog):
        with caplog.at_level(logging.DEBUG, logger="apps.credentials.resolution"):
            resolve_platform_credentials(PLATFORM, workspace=tenancy.workspace)

        assert "Skipping" not in caplog.text

    def test_unrecognised_keys_are_still_reported(self, tenancy, settings, caplog):
        """The failure this log exists for: key names that are simply wrong.

        ``PLATFORM_INSTAGRAM_CLIENTID`` (no underscore) parses into a key the
        chain does not know. The operator believes the platform is configured
        and nothing works, so silence here is the worst possible answer — the
        guard above must not widen into it.
        """
        settings.PLATFORM_CREDENTIALS_FROM_ENV = {
            PLATFORM: {"clientid": "typo-id-value", "clientsecret": "typo-secret-value"}
        }

        with caplog.at_level(logging.DEBUG, logger="apps.credentials.resolution"):
            resolution = resolve_platform_credentials(PLATFORM, workspace=tenancy.workspace)

        assert resolution.source == SOURCE_NONE
        assert "client_id/app_id" in caplog.text
        assert "client_secret/app_secret" in caplog.text
        # Key names only, never the values the operator actually typed.
        assert "typo-id-value" not in caplog.text
        assert "typo-secret-value" not in caplog.text

    def test_a_platform_with_no_app_credentials_is_never_reported(self, tenancy, settings, caplog):
        """Telegram, SMS and email have no required keys, so nothing they carry
        is a half-finished credential set."""
        settings.PLATFORM_CREDENTIALS_FROM_ENV = {"telegram": {"token": "bot-token"}}

        with caplog.at_level(logging.DEBUG, logger="apps.credentials.resolution"):
            resolve_platform_credentials("telegram", workspace=tenancy.workspace)

        assert "Skipping" not in caplog.text


@pytest.mark.django_db
class TestTenantIsolation:
    def test_one_orgs_credentials_do_not_leak_into_another(self, tenancy, other_tenancy, no_env):
        _org(tenancy, ORG_SET)

        assert resolve_platform_credentials(PLATFORM, workspace=other_tenancy.workspace).source == SOURCE_NONE
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
