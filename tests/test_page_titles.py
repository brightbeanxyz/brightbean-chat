"""One suffix for every page title.

Two conventions were live at once — half the app suffixed the workspace name,
half the product name — which shows up in browser tabs and history as two
apparently different products. `· BrightBean Chat` won on a count of 53 to 4.

A static read of the templates rather than a crawl of the routes: a title block
is a literal, every template has exactly one, and a crawl would need a fixture
per page and would still miss the ones behind a feature flag.
"""

import re
from pathlib import Path

from django.conf import settings

TITLE = re.compile(r"\{%\s*block title\s*%\}(.*?)\{%\s*endblock", re.DOTALL)

SUFFIX = "· BrightBean Chat"

#: The two base templates, which *are* the bare product name rather than
#: ending in it. Everything that extends them overrides the block.
BARE = {"base.html", "layouts/error.html"}


def _templates():
    root = Path(settings.BASE_DIR) / "templates"
    for path in sorted(root.rglob("*.html")):
        match = TITLE.search(path.read_text())
        if match:
            yield path.relative_to(root).as_posix(), " ".join(match.group(1).split())


def test_every_page_title_ends_with_the_product_name():
    wrong = {name: title for name, title in _templates() if name not in BARE and not title.endswith(SUFFIX)}

    assert wrong == {}, f"these titles use another convention: {wrong}"


def test_the_two_base_templates_are_the_bare_product_name():
    titles = {name: title for name, title in _templates() if name in BARE}

    assert titles == {name: "BrightBean Chat" for name in BARE}


def test_no_page_title_separates_with_an_em_dash():
    """The separator is a middle dot. Five error and demo pages used an em
    dash, which reads as a different product's tab beside the others."""
    dashed = {name: title for name, title in _templates() if "—" in title}

    assert dashed == {}
