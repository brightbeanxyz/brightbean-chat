"""Readers for rendered pages and template sources.

Separate from ``tests/support.py``, which builds *objects*. These read markup —
either a response body or a template file on disk — and each exists because two
test modules had grown their own copy of the same regex.

Paths come from ``settings.BASE_DIR`` rather than from counting ``__file__``
parents, so moving a test module does not silently break the read.
"""

import re
from pathlib import Path

from django.conf import settings

# `\s+` rather than the templates' literal newline and indent: re-wrapping a
# partial is a formatting change and must not fail a test about behaviour.
_TAB_ANCHOR = re.compile(r'<a href="([^"]+)"\s+class="(bb-tab[^"]*)"')
_ICON_NAME = re.compile(r'name == "([a-z_]+)"')
_TAB_INCLUDE = re.compile(r'_automations_tabs\.html"\s+with\s+automations_tab="([a-z_]*)"')


def automations_tabs(body: str) -> dict[str, str]:
    """``{href: class}`` for the Automations tab bar in a rendered page.

    The class is returned whole (``"bb-tab"`` or ``"bb-tab is-active"``) so a
    caller can assert which tab is lit and which are not in one comparison,
    rather than counting substrings and hoping.
    """
    return dict(_TAB_ANCHOR.findall(body))


def nav_icon_names() -> set[str]:
    """Every glyph ``templates/partials/_nav_icon.html`` actually draws.

    That partial answers an unknown name with a neutral dot, which is the right
    kindness for a mistyped key in a nav row that would otherwise collapse — and
    useless as a signal, because a column of dots looks like a design choice.
    Tests read this set to prove the names their code asks for are real.
    """
    source = (Path(settings.BASE_DIR) / "templates/partials/_nav_icon.html").read_text()
    return set(_ICON_NAME.findall(source))


def automations_tab_includes() -> dict[str, str]:
    """``{template path: automations_tab value}`` for every include of the tab bar.

    A value the partial does not draw renders a tab bar with nothing lit and no
    error, so the include sites are checked statically rather than relying on
    every page that ever fills the block having a test of its own.
    """
    found = {}
    for path in (Path(settings.BASE_DIR) / "templates").rglob("*.html"):
        for value in _TAB_INCLUDE.findall(path.read_text()):
            found[str(path.relative_to(settings.BASE_DIR))] = value
    return found
