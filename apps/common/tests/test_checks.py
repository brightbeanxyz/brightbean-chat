"""Deploy-safety system checks (SECURITY-BASELINE §§8, 9)."""

import pytest

from apps.common.checks import check_production_secrets, check_s3_custom_domain_signing


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
