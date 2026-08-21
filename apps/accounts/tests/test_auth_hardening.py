"""Auth rate limiting, enumeration safety and the email-verification decision."""

import pytest
from django.conf import settings
from django.core import mail

from apps.accounts.middleware import AUTH_RATE_LIMIT, AUTH_RATE_WINDOW
from tests.support import TEST_PASSWORD, create_user

LOGIN = "/accounts/login/"
SIGNUP = "/accounts/signup/"
RESET = "/accounts/password/reset/"


@pytest.mark.django_db
class TestAuthRateLimiting:
    def test_posts_are_capped(self, client):
        for _ in range(AUTH_RATE_LIMIT):
            assert client.post(LOGIN, {"login": "a@example.test", "password": "wrong"}).status_code != 429

        assert client.post(LOGIN, {"login": "a@example.test", "password": "wrong"}).status_code == 429

    def test_the_429_says_when_to_come_back(self, client):
        for _ in range(AUTH_RATE_LIMIT + 1):
            response = client.post(LOGIN, {"login": "a@example.test", "password": "wrong"})

        assert response["Retry-After"] == str(AUTH_RATE_WINDOW)

    def test_gets_are_not_limited(self, client):
        for _ in range(AUTH_RATE_LIMIT + 5):
            assert client.get(LOGIN).status_code == 200

    @pytest.mark.parametrize("path", [LOGIN, SIGNUP, RESET])
    def test_every_auth_endpoint_is_covered(self, client, path):
        for _ in range(AUTH_RATE_LIMIT + 1):
            response = client.post(path, {})

        assert response.status_code == 429

    def test_unrelated_paths_are_untouched(self, client):
        for _ in range(AUTH_RATE_LIMIT + 5):
            assert client.get("/healthz").status_code == 200

    def test_the_counter_is_shared_across_processes(self):
        """LocMemCache is per process and gunicorn runs two workers, so a
        per-process counter is evaded by landing on the other one."""
        assert "locmem" not in settings.CACHES["default"]["BACKEND"].lower()

    def test_a_forged_forwarded_header_does_not_reset_the_bucket(self, client):
        """Studio takes the leftmost X-Forwarded-For value unconditionally, so
        a new value per request means a new bucket per request."""
        for index in range(AUTH_RATE_LIMIT):
            client.post(LOGIN, {}, HTTP_X_FORWARDED_FOR=f"203.0.113.{index}")

        response = client.post(LOGIN, {}, HTTP_X_FORWARDED_FOR="203.0.113.250")
        assert response.status_code == 429

    def test_a_trusted_proxy_separates_real_clients(self, client, settings):
        settings.TRUSTED_PROXIES = ["127.0.0.1"]

        for _ in range(AUTH_RATE_LIMIT + 1):
            throttled = client.post(LOGIN, {}, HTTP_X_FORWARDED_FOR="203.0.113.1")
        fresh = client.post(LOGIN, {}, HTTP_X_FORWARDED_FOR="203.0.113.2")

        assert throttled.status_code == 429
        assert fresh.status_code != 429


@pytest.mark.django_db
class TestEnumerationSafety:
    @staticmethod
    def _errors(response):
        return sorted(str(e) for errors in response.context["form"].errors.values() for e in errors)

    def test_login_answers_the_same_for_known_and_unknown_addresses(self, client):
        create_user("real@example.test")

        known = client.post(LOGIN, {"login": "real@example.test", "password": "wrong"})
        unknown = client.post(LOGIN, {"login": "ghost@example.test", "password": "wrong"})

        assert known.status_code == unknown.status_code
        # Bodies differ only by the address echoed back into the form field.
        assert self._errors(known) == self._errors(unknown)

    def test_password_reset_answers_the_same_either_way(self, client):
        create_user("real@example.test")

        known = client.post(RESET, {"email": "real@example.test"}, follow=True)
        unknown = client.post(RESET, {"email": "ghost@example.test"}, follow=True)

        assert known.status_code == unknown.status_code
        assert known.redirect_chain == unknown.redirect_chain

    def test_the_limiter_never_reads_the_submitted_credentials(self):
        import inspect

        from apps.accounts import middleware

        source = inspect.getsource(middleware.AuthRateLimitMiddleware)
        assert "request.POST" not in source


@pytest.mark.django_db
class TestEmailVerificationIsOptional:
    def test_the_setting_is_optional(self):
        """Decided, not open (deviation 7): Studio ships "none"; "mandatory"
        would lock out a self-hoster with no SMTP."""
        assert settings.ACCOUNT_EMAIL_VERIFICATION == "optional"

    def test_a_verification_email_is_sent(self, client):
        client.post(SIGNUP, {"email": "new@example.test", "password1": TEST_PASSWORD}, follow=True)

        assert any("new@example.test" in message.to for message in mail.outbox)

    def test_signup_logs_you_straight_in(self, client):
        response = client.post(SIGNUP, {"email": "new@example.test", "password1": TEST_PASSWORD}, follow=True)

        assert response.wsgi_request.user.is_authenticated

    def test_an_smtp_outage_does_not_break_signup(self, client, monkeypatch):
        """The whole point of "optional": no SMTP must not mean no account."""

        def explode(*args, **kwargs):
            raise OSError("connection refused")

        monkeypatch.setattr("django.core.mail.message.EmailMessage.send", explode)
        monkeypatch.setattr("django.core.mail.message.EmailMultiAlternatives.send", explode)

        response = client.post(SIGNUP, {"email": "new@example.test", "password1": TEST_PASSWORD}, follow=True)

        assert response.wsgi_request.user.is_authenticated


class TestPasswordHashing:
    def test_bcrypt_sha256_is_first(self):
        """Read from base settings: config/settings/test.py swaps in MD5 so the
        suite is not spending 12 bcrypt rounds per fixture."""
        from config.settings import base

        assert base.PASSWORD_HASHERS[0] == "django.contrib.auth.hashers.BCryptSHA256PasswordHasher"

    def test_bcrypt_is_installed(self):
        """Django declares no dependency on it; the hasher fails at runtime
        without the package."""
        import bcrypt

        assert bcrypt is not None


class TestSocialLoginHardening:
    def test_provider_flows_require_post(self):
        assert settings.SOCIALACCOUNT_LOGIN_ON_GET is False

    def test_email_auto_connect_is_not_enabled(self):
        """Studio enables SOCIALACCOUNT_EMAIL_AUTHENTICATION + AUTO_CONNECT,
        which links a Google login to any local account with the same address.
        With ACCOUNT_EMAIL_VERIFICATION="optional" that is account takeover:
        register locally as victim@…, wait for their first Google login."""
        assert getattr(settings, "SOCIALACCOUNT_EMAIL_AUTHENTICATION", False) is False
        assert getattr(settings, "SOCIALACCOUNT_EMAIL_AUTHENTICATION_AUTO_CONNECT", False) is False


@pytest.mark.django_db
class TestGoogleButtonIsGated:
    def test_no_button_without_credentials(self, client):
        response = client.get(LOGIN)

        assert b"Continue with" not in response.content

    def test_the_button_appears_once_configured(self, client, settings):
        settings.GOOGLE_AUTH_CLIENT_ID = "id"
        settings.GOOGLE_AUTH_CLIENT_SECRET = "secret"  # noqa: S105

        response = client.get(LOGIN)

        assert b"Continue with" in response.content


class TestSessionSettingsAreNotRedeclared:
    def test_the_sliding_window_comes_from_layer_zero(self):
        """The brief: do not re-declare these in the allauth block."""
        import inspect

        from config.settings import base

        source = inspect.getsource(base)
        assert source.count("SESSION_COOKIE_AGE") == 1
        assert source.count("SESSION_SAVE_EVERY_REQUEST") == 1
        assert settings.SESSION_COOKIE_AGE == 14 * 24 * 60 * 60
