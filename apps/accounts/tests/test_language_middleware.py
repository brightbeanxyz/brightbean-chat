"""``LanguagePreferenceMiddleware`` and the preferences POST that feeds it.

Both were shipped with no direct coverage (Copilot review, PR #1): a
middleware-order regression or a validation slip on the preferences form would
silently revert the whole product to the wrong language for a signed-in user,
with nothing to catch it. These go through the real middleware stack via the
Django test client rather than calling the middleware directly, because the
behaviour being protected — the account's stored language beating a stale
cookie — is a property of *where* ``LanguagePreferenceMiddleware`` sits
relative to ``AuthenticationMiddleware`` and ``LocaleMiddleware``
(``config/settings/base.py``), not of the callable in isolation.

``LocaleMiddleware`` always sets the response's ``Content-Language`` header to
whatever it activated (``django.middleware.locale.LocaleMiddleware.process_response``),
so that header is the assertion surface throughout — no need to scrape
translated body text.
"""

from typing import Any

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

PREFERENCES_URL = reverse("settings_preferences")


def _login(client: Client, *, email: str, language: str) -> Any:
    user = get_user_model().objects.create_user(email=email, password="not-a-real-password", language=language)
    client.force_login(user)
    return user


@pytest.mark.django_db
class TestLanguagePreferenceMiddleware:
    def test_the_stored_preference_overrides_a_conflicting_cookie(self, client: Client) -> None:
        _login(client, email="ru-speaker@example.test", language="ru")
        client.cookies["django_language"] = "ky"

        response = client.get(PREFERENCES_URL)

        assert response["Content-Language"] == "ru"

    def test_a_blank_preference_falls_back_to_the_cookie(self, client: Client) -> None:
        _login(client, email="no-preference@example.test", language="")
        client.cookies["django_language"] = "ky"

        response = client.get(PREFERENCES_URL)

        assert response["Content-Language"] == "ky"

    def test_an_anonymous_request_is_untouched_by_the_account(self, client: Client) -> None:
        """No ``request.user`` to read yet — must not raise, must not override."""
        client.cookies["django_language"] = "ru"

        response = client.get(PREFERENCES_URL, follow=True)

        assert response["Content-Language"] == "ru"


@pytest.mark.django_db
class TestPreferencesPersistOnlySupportedCodes:
    def test_a_supported_code_is_persisted(self, client: Client) -> None:
        user = _login(client, email="picks-kyrgyz@example.test", language="")

        client.post(PREFERENCES_URL, {"language": "ky"})

        user.refresh_from_db()
        assert user.language == "ky"

    def test_an_unsupported_code_is_rejected_rather_than_stored(self, client: Client) -> None:
        user = _login(client, email="tries-french@example.test", language="ru")

        client.post(PREFERENCES_URL, {"language": "fr"})

        user.refresh_from_db()
        assert user.language == "ru"

    def test_blank_clears_a_stored_preference(self, client: Client) -> None:
        user = _login(client, email="clears-preference@example.test", language="ru")

        client.post(PREFERENCES_URL, {"language": ""})

        user.refresh_from_db()
        assert user.language == ""


@pytest.mark.django_db
class TestTheSelectOptionsRoundTrip:
    """Regression: ``LANGUAGES`` used to declare English as ``"en-us"``, a code
    Django's own ``LANG_INFO`` does not carry — ``get_language_info("en-us")``
    silently fell back to ``"en"`` and returned ``code="en"``. The template's
    ``<option value="{{ lang.code }}">`` then submitted ``"en"``, which never
    matched the ``"en-us"`` the view validated against: picking English saved
    nothing, and the "selected" comparison never matched either, so the
    dropdown looked like the click had done nothing at all.

    Parametrized over ``settings.LANGUAGES`` itself rather than hard-coding
    ``"en"``, so adding a language whose code Django's ``LANG_INFO`` normalizes
    to something else fails this test instead of shipping the same bug again.
    """

    @pytest.mark.parametrize("code", [code for code, _ in settings.LANGUAGES])
    def test_picking_a_language_persists_it_and_marks_it_selected(self, client: Client, code: str) -> None:
        user = _login(client, email=f"picks-{code}@example.test", language="")

        client.post(PREFERENCES_URL, {"language": code})

        user.refresh_from_db()
        assert user.language == code

        body = client.get(PREFERENCES_URL).content.decode()
        assert f'<option value="{code}" selected>' in body
