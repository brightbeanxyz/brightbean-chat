"""The theme registry, ``{% theme_attrs %}``, the mode rules in tokens.css and the defaults check."""

import re
from pathlib import Path

import pytest
from django.conf import settings as django_settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.template import Context, Template
from django.test import Client, RequestFactory

from apps.common import themes
from apps.common.checks import check_themes

DUO = themes.Theme("duo", "Duo", ("light", "dark"))
TOKENS = Path(django_settings.BASE_DIR) / "apps" / "theme" / "static_src" / "src" / "tokens.css"


@pytest.fixture
def duo(monkeypatch):
    """A second theme that has a dark palette, which the real registry does not yet."""
    monkeypatch.setitem(themes.THEMES, "duo", DUO)
    return DUO


def _user(**fields):
    return get_user_model().objects.create_user(email="themed@example.test", password="not-a-real-password", **fields)


class TestRegistry:
    def test_one_palette_is_one_mode(self):
        assert themes.THEMES["brightbean"].modes == ("light",)

    def test_both_palettes_add_system(self):
        """A theme offers one mode or all of them, so a mode select on screen
        always holds the stored choice and a save cannot silently drop it."""
        assert set(DUO.modes) == set(themes.COLOR_MODES)

    @pytest.mark.parametrize("palettes", [(), ("sepia",), ("light", "light")])
    def test_a_malformed_theme_cannot_be_built(self, palettes):
        """Rejected at import, so resolve() never meets one; a theme with no
        palette would otherwise raise IndexError on the error pages."""
        with pytest.raises(ValueError, match="palettes"):
            themes.Theme("bad", "Bad", palettes)

    def test_every_theme_is_keyed_by_its_own_slug(self):
        """The view validates POSTs against the key and renders the slug as the
        option value; a mismatch would be an option that can never be saved."""
        assert all(key == theme.slug for key, theme in themes.THEMES.items())


class TestModeRules:
    def test_every_mode_has_exactly_one_color_scheme_rule(self):
        """The tag prints data-color-mode; without a matching rule the mode is
        chosen and saved and changes nothing."""
        rules = re.findall(r'\[data-color-mode="([\w-]+)"\]\s*\{\s*color-scheme:', TOKENS.read_text())

        assert sorted(rules) == sorted(themes.COLOR_MODES)


class TestResolve:
    def test_blank_is_the_instance_default(self, settings):
        theme, mode = themes.resolve("", "")

        assert (theme.slug, mode) == (settings.THEME_DEFAULT, settings.COLOR_MODE_DEFAULT)

    def test_an_unknown_theme_or_mode_falls_back_rather_than_raising(self, settings):
        """A theme can be unregistered while users still have it stored."""
        theme, mode = themes.resolve("retired", "sepia")

        assert (theme.slug, mode) == (settings.THEME_DEFAULT, "light")

    @pytest.mark.parametrize("mode", ["dark", "system"])
    def test_a_mode_the_theme_has_no_tokens_for_is_clamped(self, mode):
        """``system`` on a light-only theme would give dark native controls on light tokens."""
        assert themes.resolve("brightbean", mode)[1] == "light"

    @pytest.mark.parametrize("mode", ["light", "dark", "system"])
    def test_a_supported_mode_is_kept(self, duo, mode):
        assert themes.resolve("duo", mode) == (duo, mode)

    def test_a_broken_default_still_renders(self, settings):
        """This runs on the error pages; the check is what reports the typo."""
        settings.THEME_DEFAULT = "nope"
        settings.COLOR_MODE_DEFAULT = "sepia"

        theme, mode = themes.resolve("", "")

        assert (theme.slug, mode) == ("brightbean", "light")


@pytest.mark.django_db
class TestThemeAttrsTag:
    def _render(self, **context):
        return Template("{% load common_extras %}<html {% theme_attrs %}>").render(Context(context))

    def test_a_bare_context_gets_the_default(self):
        """The 500 path: no request, no processors."""
        assert self._render() == '<html data-theme="brightbean" data-color-mode="light">'

    def test_an_anonymous_request_gets_the_default(self):
        request = RequestFactory().get("/")
        request.user = AnonymousUser()

        assert 'data-theme="brightbean"' in self._render(request=request)

    def test_a_signed_in_user_gets_their_own_choice(self, duo):
        request = RequestFactory().get("/")
        request.user = _user(theme="duo", color_mode="system")

        assert self._render(request=request) == '<html data-theme="duo" data-color-mode="system">'

    def test_a_404_for_a_signed_in_user_carries_their_choice(self, duo, client: Client):
        """The error layout does not extend base.html; it gets there by its own path."""
        client.force_login(_user(theme="duo", color_mode="dark"))

        response = client.get("/no-such-page")

        assert response.status_code == 404
        assert 'data-theme="duo" data-color-mode="dark"' in response.content.decode()


class TestThemesCheck:
    def test_the_shipped_defaults_pass(self):
        assert check_themes() == []

    @pytest.mark.parametrize(
        ("theme", "mode", "needle"),
        [
            ("nope", "light", "THEME_DEFAULT 'nope'"),
            ("brightbean", "sepia", "COLOR_MODE_DEFAULT 'sepia' is not a colour mode"),
            ("brightbean", "dark", "not supported by theme 'brightbean'"),
        ],
    )
    def test_a_default_that_would_be_silently_replaced_is_an_error(self, settings, theme, mode, needle):
        settings.THEME_DEFAULT = theme
        settings.COLOR_MODE_DEFAULT = mode

        (error,) = check_themes()

        assert error.id == "common.E007"
        assert needle in error.msg
