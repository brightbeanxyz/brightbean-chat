"""The colour roles (docs/design/HANDOFF.md §2.1), held in place.

Orange is the signal for one thing: the screen's primary action. The only other
place it may appear is the focus ring. Everything that used to borrow it to say
"selected", "current", "unread" or "sending" says so in ink, green, amber, blue
or red now, because a second orange control on a screen reads as a second call
to action.

That rule is easy to state and easy to erode one rule at a time — the redesign
found about thirty places it had already been broken, each of them reasonable
on its own. So it is a test: any read of an orange token outside the short
allowlist below fails here, and adding to the allowlist is a decision someone
has to argue for in review rather than something that slips in.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).parents[3]
STYLES = REPO / "apps" / "theme" / "static_src" / "src" / "styles.css"

# Every token that resolves to the brand orange. --brand-green-* is the bean
# green and --brand-font-* a typeface, so both are excluded; --cta is the
# primary button's fill and belongs here like the rest.
ORANGE = re.compile(r"var\(--(?:primary[\w-]*|cta[\w-]*|shadow-primary|brand-(?!green-|font-)[\w-]+)\)")

# The selectors allowed to read one. Each entry is a regex matched against one
# selector of a rule's (comma-separated) selector list; every selector in the
# list has to match, so a shared rule cannot smuggle a new one in.
ALLOWED_SELECTORS = (
    # The token block itself.
    r"^:root$",
    # The primary action, in its three spellings: the app's pill button, the
    # auth pages' full-width button, and allauth's classless submit.
    r"^\.btn-pill-primary\b",
    r"^\.btn-brand\b",
    r"^\.auth-card button\[type=\"submit\"\]:not\(\[class\]\)",
    # The auth pages' background wash: brand atmosphere on a page whose only
    # control in colour is the submit.
    r"^\.auth-bg$",
    # The workspace's logo mark is the brand, not a control.
    r"^\.sidebar-logo-mark$",
    # Focus rings — the one orange outside a primary action.
    r":focus(?:-visible|-within)?\b",
)


def _rules(css: str) -> list[tuple[str, str]]:
    """(selector list, declarations) for every innermost rule in ``css``.

    Comments are dropped first. Nested at-rules (@media, @layer, @supports) are
    walked through, and only the block that actually holds declarations is
    returned, with its own selector list. The stylesheet has no braces inside
    strings or url()s, which is the one thing a parser this small cannot see.
    """
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    rules: list[tuple[str, str]] = []
    stack: list[str] = []
    buf = ""
    for ch in css:
        if ch == "{":
            stack.append(buf.strip())
            buf = ""
        elif ch == "}":
            prelude = stack.pop() if stack else ""
            if buf.strip():
                rules.append((prelude, buf))
            buf = ""
        elif ch == ";" and not stack:
            # A top-level statement (@import, @source) ends here; without this
            # it would be glued onto the next rule's selector.
            buf = ""
        else:
            buf += ch
    return rules


def _allowed(selector_list: str) -> bool:
    selectors = [s.strip() for s in selector_list.split(",") if s.strip()]
    return bool(selectors) and all(any(re.search(p, s) for p in ALLOWED_SELECTORS) for s in selectors)


def test_orange_is_read_only_by_primary_actions_and_focus_rings():
    offenders = [
        f"{prelude} {{ {' '.join(ORANGE.findall(body))} }}"
        for prelude, body in _rules(STYLES.read_text())
        if ORANGE.search(body) and not _allowed(prelude)
    ]

    assert not offenders, (
        "These rules paint something orange that is neither a primary action nor a focus ring. "
        "Use ink (--ink, --selected-fill) for selected/current, or a status tone, instead:\n  " + "\n  ".join(offenders)
    )


def test_no_template_or_builder_component_reaches_for_orange_inline():
    """An inline style="var(--primary)" is the same mistake out of the
    stylesheet's sight. The style guide's palette swatches are the exception:
    showing the brand ramp is their whole job."""
    exempt = {REPO / "templates" / "ui_demo.html"}
    sources = [
        *sorted((REPO / "templates").rglob("*.html")),
        *sorted((REPO / "apps" / "flows" / "frontend" / "src").rglob("*.tsx")),
    ]
    offenders = [
        f"{path.relative_to(REPO)}:{number}"
        for path in sources
        if path not in exempt
        for number, line in enumerate(path.read_text().splitlines(), start=1)
        if ORANGE.search(line)
    ]

    assert not offenders, "Inline orange outside the stylesheet's allowlist:\n  " + "\n  ".join(offenders)


def test_the_parser_sees_rules_inside_at_rules_and_skips_comments():
    """The guard is only as good as its parser, so pin the two shapes that
    matter: a rule nested in @media/@layer, and a comment that mentions a
    token without using it."""
    css = """
    @import "tailwindcss" source(none);
    /* .x { color: var(--primary); } */
    @layer base { .app-content a { color: var(--primary); } }
    @media (min-width: 1px) { .btn-pill-primary { background: var(--cta); } }
    """

    rules = _rules(css)

    assert rules == [
        (".app-content a", " color: var(--primary); "),
        (".btn-pill-primary", " background: var(--cta); "),
    ]
    assert not _allowed(".app-content a")
    assert _allowed(".btn-pill-primary")
    assert _allowed(".search-input:focus")
    assert not _allowed(".btn-pill-primary, .chip-filter.active")


def test_the_primary_button_fills_with_the_contrast_safe_orange():
    """White on --brand-500 is 2.8:1; on --brand-600 it is 3.6:1. The button
    reads --cta, and --cta is the darker step."""
    css = STYLES.read_text()
    root = css[css.index(":root {") : css.index("}", css.index(":root {"))]

    assert "--cta:          var(--brand-600);" in root
    assert any(prelude == ".btn-pill-primary" and "var(--cta)" in body for prelude, body in _rules(css)), (
        ".btn-pill-primary no longer fills with --cta"
    )
