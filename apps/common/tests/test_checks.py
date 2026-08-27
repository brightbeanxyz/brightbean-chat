"""Deploy-safety system checks (SECURITY-BASELINE §§8, 9)."""

import pytest

from apps.common.checks import (
    MIN_WEBHOOK_LOG_RETENTION_DAYS,
    check_production_secrets,
    check_s3_custom_domain_signing,
    check_webhook_log_retention,
)


def _ids(messages):
    return {message.id for message in messages}


@pytest.mark.django_db
class TestProductionSecrets:
    def test_silent_in_debug(self, settings):
        settings.DEBUG = True
        settings.SECRET_KEY = "change-me-to-a-random-string"

        assert check_production_secrets() == []

    def test_placeholder_secret_key_is_an_error(self, settings):
        settings.DEBUG = False
        settings.SECRET_KEY = "change-me-to-a-random-string"

        assert "common.E001" in _ids(check_production_secrets())

    def test_placeholder_salt_is_an_error(self, settings):
        settings.DEBUG = False
        settings.ENCRYPTION_KEY_SALT = b"django-insecure-dev-only-salt-not-for-production"

        assert "common.E002" in _ids(check_production_secrets())

    def test_empty_allowed_hosts_is_an_error(self, settings):
        settings.DEBUG = False
        settings.ALLOWED_HOSTS = []

        assert "common.E003" in _ids(check_production_secrets())

    def test_whitespace_only_hosts_do_not_count(self, settings):
        settings.DEBUG = False
        settings.ALLOWED_HOSTS = ["   ", ""]

        assert "common.E003" in _ids(check_production_secrets())

    def test_clean_production_settings_pass(self, settings):
        settings.DEBUG = False
        settings.SECRET_KEY = "Zq4tPmXk9BvRnLwCyHsDfGjKaEuT7NbM2VxQ"
        settings.ENCRYPTION_KEY_SALT = b"VxQ2NbM7EuTaKjGfDsHyCwLnRvB9kXmPt4qZ"
        settings.ALLOWED_HOSTS = ["chat.example.com"]

        assert check_production_secrets() == []


@pytest.mark.django_db
class TestS3CustomDomainSigning:
    """django-storages returns unsigned URLs on the custom-domain path unless a
    CloudFront signer is configured, so a private bucket behind a custom domain
    hands out links that 403 (SECURITY-BASELINE §9 wants them signed)."""

    def test_silent_for_local_storage(self, settings):
        settings.STORAGE_BACKEND = "local"

        assert check_s3_custom_domain_signing() == []

    def test_silent_without_a_custom_domain(self, settings):
        settings.STORAGE_BACKEND = "s3"
        settings.AWS_S3_CUSTOM_DOMAIN = ""
        settings.AWS_QUERYSTRING_AUTH = True

        assert check_s3_custom_domain_signing() == []

    def test_warns_on_custom_domain_without_a_signer(self, settings):
        settings.STORAGE_BACKEND = "s3"
        settings.AWS_S3_CUSTOM_DOMAIN = "cdn.example.com"
        settings.AWS_QUERYSTRING_AUTH = True
        settings.AWS_CLOUDFRONT_KEY_ID = ""
        settings.AWS_CLOUDFRONT_KEY = ""

        assert "common.W001" in _ids(check_s3_custom_domain_signing())

    def test_silent_once_a_signer_is_configured(self, settings):
        settings.STORAGE_BACKEND = "s3"
        settings.AWS_S3_CUSTOM_DOMAIN = "cdn.example.com"
        settings.AWS_QUERYSTRING_AUTH = True
        settings.AWS_CLOUDFRONT_KEY_ID = "APKAEXAMPLE"
        settings.AWS_CLOUDFRONT_KEY = "-----BEGIN RSA PRIVATE KEY-----..."

        assert check_s3_custom_domain_signing() == []

    def test_silent_for_deliberately_public_delivery(self, settings):
        """Unsigned URLs are the point when querystring auth is off."""
        settings.STORAGE_BACKEND = "s3"
        settings.AWS_S3_CUSTOM_DOMAIN = "cdn.example.com"
        settings.AWS_QUERYSTRING_AUTH = False

        assert check_s3_custom_domain_signing() == []

    def test_only_one_of_the_signer_pair_still_warns(self, settings):
        settings.STORAGE_BACKEND = "s3"
        settings.AWS_S3_CUSTOM_DOMAIN = "cdn.example.com"
        settings.AWS_QUERYSTRING_AUTH = True
        settings.AWS_CLOUDFRONT_KEY_ID = "APKAEXAMPLE"
        settings.AWS_CLOUDFRONT_KEY = ""

        assert "common.W001" in _ids(check_s3_custom_domain_signing())


class TestWebhookLogRetentionFloor:
    """Issue #95: the retention dial is also the replay-protection memory.

    ``WebhookEventLog``'s unique ``(connection, provider_event_id)`` is what makes
    a redelivered webhook a no-op, so a pruned row is a delivery id this
    deployment has forgotten. An operator shortening the window for privacy is
    doing something reasonable; shortening it below every platform's redelivery
    window is silently accepting duplicate sends, and that should not be silent.
    """

    def test_the_default_is_accepted(self, settings):
        settings.WEBHOOK_EVENT_LOG_RETENTION_DAYS = 30

        assert check_webhook_log_retention() == []

    def test_the_floor_itself_is_accepted(self, settings):
        settings.WEBHOOK_EVENT_LOG_RETENTION_DAYS = MIN_WEBHOOK_LOG_RETENTION_DAYS

        assert check_webhook_log_retention() == []

    @pytest.mark.parametrize("days", [0, 1, 6])
    def test_below_the_floor_is_an_error(self, settings, days):
        settings.WEBHOOK_EVENT_LOG_RETENTION_DAYS = days

        assert "common.E006" in _ids(check_webhook_log_retention())

    def test_the_hint_says_why_rather_than_just_what(self, settings):
        """An operator who shortened this on purpose needs the reason, not the rule."""
        settings.WEBHOOK_EVENT_LOG_RETENTION_DAYS = 1

        hint = check_webhook_log_retention()[0].hint

        assert "redelivered" in hint and "twice" in hint

    def test_the_floor_clears_every_platforms_redelivery_window(self):
        """Meta retries for about 36 hours; the others for less. Seven days has room."""
        assert MIN_WEBHOOK_LOG_RETENTION_DAYS >= 2
