"""PlatformCredential / WorkspaceCredentialOverride invariants."""

import pytest

from apps.common.encryption import decrypt_value
from apps.common.platforms import Platform
from apps.credentials.models import (
    REQUIRED_CREDENTIAL_KEYS,
    PlatformCredential,
    WorkspaceCredentialOverride,
    derive_is_configured,
    mask_credentials,
    missing_key_groups,
)

COMPLETE = {"client_id": "id-12345", "client_secret": "secret-67890"}


class TestDeriveIsConfigured:
    def test_a_complete_set_configures(self):
        assert derive_is_configured("instagram", COMPLETE)

    def test_an_alias_satisfies_its_group(self):
        """Meta's docs say app_id/app_secret; its OAuth endpoints say client_*."""
        assert derive_is_configured("instagram", {"app_id": "a", "app_secret": "b"})

    def test_a_missing_group_does_not(self):
        assert not derive_is_configured("instagram", {"client_id": "a"})

    def test_a_blank_value_does_not(self):
        assert not derive_is_configured("instagram", {"client_id": "a", "client_secret": "   "})

    def test_a_none_value_does_not(self):
        """str(None) is "None", which is truthy after .strip() — the trap the
        `or ""` in derive_is_configured exists to close."""
        assert not derive_is_configured("instagram", {"client_id": "a", "client_secret": None})

    @pytest.mark.parametrize("platform", ["telegram", "sms", "email"])
    def test_platforms_without_app_credentials_never_configure(self, platform):
        """Telegram bot tokens, Twilio SIDs and SMTP logins are per connection,
        not per deployment — the same shape as Studio's session-auth platforms."""
        assert platform not in REQUIRED_CREDENTIAL_KEYS
        assert not derive_is_configured(platform, COMPLETE)

    def test_missing_groups_name_keys_never_values(self):
        groups = missing_key_groups("instagram", {"client_id": "id-12345"})

        assert groups == [("client_secret", "app_secret")]

    def test_every_configurable_platform_is_a_known_platform(self):
        assert set(REQUIRED_CREDENTIAL_KEYS) <= {choice.value for choice in Platform}


class TestMasking:
    def test_only_the_last_four_characters_survive(self):
        assert mask_credentials({"client_secret": "abcdefghij"}) == {"client_secret": "****ghij"}

    def test_short_values_are_fully_masked(self):
        assert mask_credentials({"pin": "1234"}) == {"pin": "****"}

    def test_non_strings_are_fully_masked(self):
        assert mask_credentials({"n": 12345}) == {"n": "****"}


@pytest.mark.django_db
class TestSaveInvariant:
    def test_is_configured_is_derived_not_assigned(self, tenancy):
        credential = PlatformCredential.objects.create(
            organization=tenancy.organization,
            platform="instagram",
            credentials={"client_id": "a"},
            is_configured=True,
        )

        assert credential.is_configured is False

    def test_a_partial_save_still_recomputes_the_flag(self, tenancy):
        """save(update_fields=["credentials"]) would otherwise write new secrets
        and leave the stale flag behind."""
        credential = PlatformCredential.objects.create(
            organization=tenancy.organization, platform="instagram", credentials={}
        )
        credential.credentials = COMPLETE
        credential.save(update_fields=["credentials"])

        credential.refresh_from_db()
        assert credential.is_configured is True

    def test_one_row_per_org_and_platform(self, tenancy):
        from django.db import IntegrityError

        PlatformCredential.objects.create(organization=tenancy.organization, platform="instagram", credentials=COMPLETE)

        with pytest.raises(IntegrityError):
            PlatformCredential.objects.create(
                organization=tenancy.organization, platform="instagram", credentials=COMPLETE
            )


@pytest.mark.django_db
class TestEncryptionAtRest:
    def test_the_column_holds_ciphertext(self, tenancy):
        from django.db import connection

        credential = PlatformCredential.objects.create(
            organization=tenancy.organization, platform="instagram", credentials=COMPLETE
        )

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT credentials FROM credentials_platform_credential WHERE id = %s", [str(credential.pk)]
            )
            raw = cursor.fetchone()[0]

        assert "secret-67890" not in raw
        assert decrypt_value(raw)

    def test_it_round_trips(self, tenancy):
        credential = PlatformCredential.objects.create(
            organization=tenancy.organization, platform="instagram", credentials=COMPLETE
        )

        assert PlatformCredential.objects.get(pk=credential.pk).credentials == COMPLETE


@pytest.mark.django_db
class TestTheEncryptedColumnIsNotFilterable:
    """Why there is no HMAC sidecar column here.

    ``apps.common.encryption``'s docstring prescribes a deterministic sidecar
    for looking a row up *by* a secret. Nothing does that here — both tables are
    keyed on (tenant, platform), which are plaintext columns. The first real
    consumer is issue #4's webhook-secret lookup. This test pins the failure
    mode that makes the alternative dangerous.
    """

    def test_filtering_on_credentials_silently_matches_nothing(self, tenancy):
        PlatformCredential.objects.create(organization=tenancy.organization, platform="instagram", credentials=COMPLETE)

        # No exception — just an empty result that reads like "no such row".
        assert PlatformCredential.objects.filter(credentials=COMPLETE).count() == 0

    def test_the_supported_lookup_works(self, tenancy):
        PlatformCredential.objects.create(organization=tenancy.organization, platform="instagram", credentials=COMPLETE)

        assert PlatformCredential.objects.for_org(tenancy.organization.pk).filter(platform="instagram").exists()

    def test_the_model_documents_it(self):
        assert "never looked up by their contents" in (
            __import__("apps.credentials.models", fromlist=["x"]).__doc__ or ""
        )


@pytest.mark.django_db
class TestWorkspaceOverrideIsTenantScoped:
    def test_it_uses_the_enforcing_manager(self, tenancy):
        from apps.common.scoping import UnscopedQueryError

        WorkspaceCredentialOverride.objects.create(
            workspace=tenancy.workspace, platform="instagram", credentials=COMPLETE
        )

        with pytest.raises(UnscopedQueryError):
            WorkspaceCredentialOverride.objects.count()

    def test_one_row_per_workspace_and_platform(self, tenancy):
        from django.db import IntegrityError

        WorkspaceCredentialOverride.objects.create(
            workspace=tenancy.workspace, platform="instagram", credentials=COMPLETE
        )

        with pytest.raises(IntegrityError):
            WorkspaceCredentialOverride.objects.create(
                workspace=tenancy.workspace, platform="instagram", credentials=COMPLETE
            )
