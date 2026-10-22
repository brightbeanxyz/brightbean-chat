"""Theme and colour mode on the preferences page.

The language half of the same form is covered in test_language_middleware.py.
"""

from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

from apps.common import themes

PREFERENCES_URL = reverse("settings_preferences")


def _login(client: Client, **fields: Any) -> Any:
    user = get_user_model().objects.create_user(email="prefers@example.test", password="not-a-real-password", **fields)
    client.force_login(user)
    return user


@pytest.fixture
def duo(monkeypatch):
    """What Phase 2 and 3 will register: a second theme, and more than one mode."""
    monkeypatch.setitem(themes.THEMES, "duo", themes.Theme("duo", "Duo", ("light", "dark")))


@pytest.mark.django_db
class TestSaving:
    def test_a_registered_theme_and_mode_are_persisted(self, client: Client) -> None:
        user = _login(client)

        client.post(PREFERENCES_URL, {"theme": "brightbean", "color_mode": "system"})

        user.refresh_from_db()
        assert (user.theme, user.color_mode) == ("brightbean", "system")

    def test_unknown_values_are_rejected_rather_than_stored(self, client: Client) -> None:
        user = _login(client, theme="brightbean", color_mode="light")

        client.post(PREFERENCES_URL, {"theme": "nope", "color_mode": "sepia"})

        user.refresh_from_db()
        assert (user.theme, user.color_mode) == ("brightbean", "light")

    def test_blank_clears_back_to_the_default(self, client: Client) -> None:
        user = _login(client, theme="brightbean", color_mode="light")

        client.post(PREFERENCES_URL, {"theme": "", "color_mode": ""})

        user.refresh_from_db()
        assert (user.theme, user.color_mode) == ("", "")

    def test_saving_the_language_leaves_the_theme_alone(self, client: Client) -> None:
        """The theme selects are absent while they offer no choice, so a language
        save posts no theme key at all. Reading that as "" would reset them."""
        user = _login(client, theme="brightbean", color_mode="system")

        client.post(PREFERENCES_URL, {"language": "ru"})

        user.refresh_from_db()
        assert (user.language, user.theme, user.color_mode) == ("ru", "brightbean", "system")


@pytest.mark.django_db
class TestTheSelects:
    def test_no_select_while_there_is_nothing_to_choose(self, client: Client) -> None:
        _login(client)

        body = client.get(PREFERENCES_URL).content.decode()

        assert 'name="theme"' not in body
        assert 'name="color_mode"' not in body

    def test_both_appear_and_mark_the_stored_choice(self, client: Client, duo: None) -> None:
        _login(client, theme="duo", color_mode="dark")

        body = client.get(PREFERENCES_URL).content.decode()

        assert '<option value="duo" selected>' in body
        assert '<option value="dark" selected>' in body

    def test_the_mode_select_offers_only_what_the_current_theme_supports(self, client: Client, duo: None) -> None:
        _login(client, theme="brightbean")

        body = client.get(PREFERENCES_URL).content.decode()

        assert 'name="theme"' in body
        assert 'name="color_mode"' not in body
