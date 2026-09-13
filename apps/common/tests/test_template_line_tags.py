"""A Django tag cannot be split across two lines, and failing at it is silent.

``django.template.base.tag_re`` is compiled **without** ``re.DOTALL``:

    tag_re = _lazy_re_compile(r"({%.*?%}|{{.*?}}|{#.*?#})")

So ``{{ items|join:"`` on one line and ``" }}`` on the next is not a malformed
tag — it is not a tag. Django prints it as text, no exception, no warning, and
the page looks fine until somebody reads what is in the box.

It reached production once. ``flows/triggers/_form_comment.html`` used exactly
that shape for four ``<textarea>`` fields, so opening a comment trigger for
editing filled them with the template's own source, and saving stored
``{{ config.post_ids|join:"`` as a post id. The trigger came out scoped to posts
that do not exist, which matches nothing and explains nothing.

Two tests, because one alone would not have caught it: the sweep is the guard
against the shape coming back anywhere, and the filter test is the guard against
the replacement quietly regressing.
"""

import re
from pathlib import Path

from django.conf import settings
from django.template.loader import render_to_string

from apps.common.templatetags.common_extras import lines

#: Django's own lexer, copied rather than imported: the point is to fail here if
#: the real one ever changes, not to agree with it by construction.
TAG_RE = re.compile(r"({%.*?%}|{{.*?}}|{#.*?#})")
OPENER_RE = re.compile(r"\{\{|\{%")


def _templates() -> list[Path]:
    return sorted(Path(settings.BASE_DIR).joinpath("templates").rglob("*.html"))


class TestNoTemplateSplitsATagAcrossLines:
    def test_every_opener_closes_on_its_own_line(self) -> None:
        offenders = []
        for path in _templates():
            leftover = TAG_RE.sub("", path.read_text())
            for number, line in enumerate(leftover.splitlines(), 1):
                if OPENER_RE.search(line):
                    offenders.append(f"{path.name}:{number}: {line.strip()[:80]}")

        assert offenders == [], (
            "These lines open a Django tag that does not close on the same line, "
            "so Django prints them verbatim instead of rendering them. To join a "
            "list onto separate lines use the `lines` filter: " + "; ".join(offenders)
        )

    def test_the_comment_trigger_form_fills_its_boxes_with_values(self) -> None:
        """The regression itself, at the surface that shipped it."""
        html = render_to_string(
            "flows/triggers/_form_comment.html",
            {
                "config": {
                    "post_ids": ["17900000000000000"],
                    "include_keywords": ["PRICE", "INFO"],
                    "exclude_keywords": [],
                    "public_reply": {"texts": ["Just sent you a DM!"]},
                }
            },
        )

        assert "PRICE\nINFO" in html
        assert "17900000000000000" in html
        assert "join:" not in html


class TestTheLinesFilter:
    def test_it_puts_one_item_per_line(self) -> None:
        assert lines(["a", "b"]) == "a\nb"

    def test_it_stringifies_so_ids_read_like_words(self) -> None:
        assert lines([1, 2]) == "1\n2"

    def test_a_string_is_not_a_list_of_characters(self) -> None:
        assert lines("abc") == ""

    def test_a_missing_value_fills_the_box_with_nothing(self) -> None:
        """Not its repr: whatever lands here is saved back as content."""
        assert lines(None) == ""
