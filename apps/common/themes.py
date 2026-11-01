"""The theme registry: which themes exist and which colour modes each supports.

Two independent axes (docs/themes-roadmap.md): a **theme** is a palette, fonts
and radii, printed as ``data-theme`` on ``<html>``; a **mode** is light, dark or
system, printed as ``data-color-mode``. tokens.css turns the mode into the
``color-scheme`` property, which is what every ``light-dark()`` token resolves
against. *System* is not a third palette, it is ``color-scheme: light dark``
and lets the OS pick.

This module is the one list. The preferences view validates against it, the
``{% theme_attrs %}`` tag resolves through it, ``apps.common.checks`` checks the
instance defaults against it, and the tests read it rather than restating it.
"""

from dataclasses import dataclass

from django.conf import settings
from django.utils.translation import gettext_lazy as _

#: The palettes a theme can ship tokens for.
PALETTES = ("light", "dark")

#: mode → its label. Each mode has a ``[data-color-mode]`` rule in tokens.css
#: (apps/common/tests/test_themes.py holds the two lists together).
COLOR_MODES = {
    "light": _("Light"),
    "dark": _("Dark"),
    "system": _("Match my system"),
}


@dataclass(frozen=True)
class Theme:
    slug: str
    #: A product name, so it is not translated.
    label: str
    #: The palettes this theme has tokens for, from :data:`PALETTES`. The first
    #: one is where an unsupported mode lands.
    palettes: tuple[str, ...]

    def __post_init__(self) -> None:
        # A malformed entry fails at import, i.e. at boot, rather than on the
        # first page that renders it, which could be the 500 page.
        if (
            not self.palettes
            or len(set(self.palettes)) != len(self.palettes)
            or not set(self.palettes) <= set(PALETTES)
        ):
            raise ValueError(f"Theme {self.slug!r}: palettes must be one or both of {PALETTES}, got {self.palettes!r}")

    @property
    def modes(self) -> tuple[str, ...]:
        """Derived rather than declared: *system* means "either palette", so it
        exists exactly when both do. A theme therefore offers either one mode or
        all of them, and a stored mode is never missing from a select that is
        on screen."""
        return self.palettes + (("system",) if len(self.palettes) > 1 else ())


def _registry(*themes: Theme) -> dict[str, Theme]:
    """Keyed by each theme's own slug, so the key and the slug cannot disagree."""
    return {theme.slug: theme for theme in themes}


THEMES = _registry(
    Theme("brightbean", "BrightBean", ("light",)),
)


def resolve(theme_slug: str, mode: str) -> tuple[Theme, str]:
    """A stored preference → the theme and the mode to render.

    Never raises, whatever the settings say: this runs on the error pages too.
    A bad instance default is reported by ``apps.common.checks``, not by a
    ``KeyError`` on every page, and a malformed theme cannot get this far
    (``Theme.__post_init__``).

    Blank or unknown falls back to the instance default. A mode the theme has no
    tokens for falls back to the default mode, or failing that the theme's first
    palette. Only here, at render time: the stored choice is left alone, so
    someone who picked *system* gets it the moment their theme grows a dark
    palette.
    """
    theme = THEMES.get(theme_slug) or THEMES.get(settings.THEME_DEFAULT) or next(iter(THEMES.values()))
    if mode not in theme.modes:
        mode = settings.COLOR_MODE_DEFAULT
    if mode not in theme.modes:
        mode = theme.modes[0]
    return theme, mode
