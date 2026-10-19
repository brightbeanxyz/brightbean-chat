"""Colour lives in the token file and nowhere else.

The theme system (docs/themes-roadmap.md) swaps a whole palette by overriding
tokens. That only works if every colour on screen is *read from* a token: a
literal ``#fff`` in a component rule, or a template's inline ``rgba(…)``, is a
spot the next theme silently cannot reach. Before this file the rule was a
comment in styles.css, and the codebase had drifted from it — six literal
whites, two hard-coded inks, and three tokens that were referenced but never
defined, so the browser dropped the declaration without a word.

Three guards:

* **No colour literals outside tokens.css** — in component CSS, in template
  ``style`` attributes, ``<style>`` and ``<script>`` blocks, and in the
  flow-builder source.
  Email templates are exempt: mail clients do not support custom properties, so
  an email carries its colours inline by necessity.
* **Every ``var(--x)`` is defined somewhere.** An undefined custom property is
  not an error in CSS; the declaration just stops applying.
* **Every ``<html>`` root calls ``{% theme_attrs %}``**, which prints the
  ``data-theme`` and ``data-color-mode`` the tokens resolve against.

# ponytail: regex over source text, not a CSS parser. Declarations are matched
# as ``prop: value`` up to ``;``/``}``, which is what this repo's CSS looks like;
# a value containing a literal ``;`` inside a string would be split early, and
# that only ever under-reports. Swap in a real parser if that ever bites.
"""

import re
from pathlib import Path

from django.conf import settings

ROOT = Path(settings.BASE_DIR)
CSS_DIR = ROOT / "theme" / "static_src" / "src"
#: Shared with tests/test_theme_contrast.py: TOKENS, _COMMENT, _VAR_DEF and
#: _blank_comments below are imported there rather than reimplemented (found
#: in review of PR #7 — nothing here said so before). Renaming or changing
#: what any of them do is a cross-file break, not just a local one.
TOKENS = CSS_DIR / "tokens.css"
STYLES = CSS_DIR / "styles.css"
TEMPLATES = ROOT / "templates"
BUILDER = ROOT / "frontend" / "builder" / "src"

#: Prefixes owned by someone else: Tailwind's internals, and xyflow's defaults
#: (defined by the vendored builder.css, a gitignored build artefact).
FOREIGN_PREFIXES = ("--tw-", "--xy-")

_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_DECLARATION = re.compile(r"(--[\w-]+|[a-z-]+)\s*:\s*([^;{}]+)")
#: All 148 CSS named colours (CSS Color 4). Not ``transparent`` or ``currentColor``:
#: those carry no palette value, and ``transparent`` is what every ``color-mix()`` wash
#: mixes into.
# A word list reads better than 148 one-per-line literals.
NAMED_COLOURS = frozenset(
    """
    aliceblue antiquewhite aqua aquamarine azure beige bisque black blanchedalmond blue
    blueviolet brown burlywood cadetblue chartreuse chocolate coral cornflowerblue
    cornsilk crimson cyan darkblue darkcyan darkgoldenrod darkgray darkgreen darkgrey
    darkkhaki darkmagenta darkolivegreen darkorange darkorchid darkred darksalmon
    darkseagreen darkslateblue darkslategray darkslategrey darkturquoise darkviolet
    deeppink deepskyblue dimgray dimgrey dodgerblue firebrick floralwhite forestgreen
    fuchsia gainsboro ghostwhite gold goldenrod gray green greenyellow grey honeydew
    hotpink indianred indigo ivory khaki lavender lavenderblush lawngreen lemonchiffon
    lightblue lightcoral lightcyan lightgoldenrodyellow lightgray lightgreen lightgrey
    lightpink lightsalmon lightseagreen lightskyblue lightslategray lightslategrey
    lightsteelblue lightyellow lime limegreen linen magenta maroon mediumaquamarine
    mediumblue mediumorchid mediumpurple mediumseagreen mediumslateblue
    mediumspringgreen mediumturquoise mediumvioletred midnightblue mintcream mistyrose
    moccasin navajowhite navy oldlace olive olivedrab orange orangered orchid
    palegoldenrod palegreen paleturquoise palevioletred papayawhip peachpuff peru pink
    plum powderblue purple rebeccapurple red rosybrown royalblue saddlebrown salmon
    sandybrown seagreen seashell sienna silver skyblue slateblue slategray slategrey
    snow springgreen steelblue tan teal thistle tomato turquoise violet wheat white
    whitesmoke yellow yellowgreen
    """.split()  # noqa: SIM905
)
_NAMED = "(?:" + "|".join(sorted(NAMED_COLOURS, key=len, reverse=True)) + ")"
#: ``%23`` is ``#`` inside a url-encoded data-URI (an inline SVG's ``stroke='%23fff'``).
#: CSS keywords and function names are case-insensitive, hence ``re.I``.
#: Every CSS colour function. ``color()`` is the wide-gamut one (``color(display-p3 1 0 0)``);
#: ``color-mix()`` is not in the list, it only mixes tokens.
_FUNCTIONS = r"(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch|color)"
_COLOUR = re.compile(
    r"(?:#|%23)[0-9a-f]{3,8}\b"
    rf"|\b{_FUNCTIONS}\(\s*[\w.]"
    rf"|(?<![-\w]){_NAMED}(?![-\w])",
    re.I,
)
#: HTML attribute names are case-insensitive and may have whitespace around ``=``.
_STYLE_ATTR = re.compile(r"""\bstyle\s*=\s*(?P<q>["'])(?P<body>.*?)(?P=q)""", re.DOTALL | re.I)
_STYLE_BLOCK = re.compile(r"<style[^>]*>(?P<body>.*?)</style>", re.DOTALL)
_SCRIPT_BLOCK = re.compile(r"<script[^>]*>(?P<body>.*?)</script>", re.DOTALL)
_TEMPLATE_COMMENT = re.compile(r"{%\s*comment\s*%}.*?{%\s*endcomment\s*%}|{#.*?#}", re.DOTALL)
#: A name followed by ``{{`` is assembled by the template (``var(--platform-{{ key }}, …)``)
#: and cannot be checked statically; the lookahead skips it without backtracking into a
#: shorter, bogus name.
_VAR_REF = re.compile(r"var\(\s*(--[\w-]+)(?![\w-]|\{\{)|token\(\s*\"(--[\w-]+)\"")
_VAR_DEF = re.compile(r"(--[\w-]+)\s*:")
#: A string literal whose whole value is a colour: the same hex, functions and names as
#: :data:`_COLOUR`.
_JS_COLOUR = re.compile(rf"""["'`](?:#[0-9a-f]{{3,8}}|{_FUNCTIONS}\([^"'`]*|{_NAMED})["'`]""", re.I)


def _line(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _blank_comments(text: str, pattern: re.Pattern[str]) -> str:
    """Drop comments but keep line numbers, so failures point at real lines."""
    return pattern.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)


def _colour_literals_in_css(css: str, base: int = 0) -> list[tuple[int, str]]:
    found = []
    for decl in _DECLARATION.finditer(css):
        if _COLOUR.search(decl.group(2)):
            found.append((_line(css, decl.start()) + base, decl.group(0).strip()))
    return found


def _templates() -> list[Path]:
    return sorted(p for p in TEMPLATES.rglob("*.html") if "email" not in p.parent.parts)


def _builder_sources() -> list[Path]:
    return sorted(p for p in BUILDER.rglob("*") if p.suffix in {".ts", ".tsx"} and ".test." not in p.name)


def _format(offenders: list[str], advice: str) -> str:
    return f"{len(offenders)} offender(s) — {advice}\n  " + "\n  ".join(offenders)


class TestNoColourLiterals:
    def test_the_token_file_exists_and_is_imported(self):
        assert TOKENS.exists(), "theme/static_src/src/tokens.css is where colour values live"
        assert '@import "./tokens.css"' in STYLES.read_text(), "styles.css must @import tokens.css"

    def test_component_css_has_no_colour_literals(self):
        css = _blank_comments(STYLES.read_text(), _COMMENT)
        offenders = [f"styles.css:{n}: {decl}" for n, decl in _colour_literals_in_css(css)]

        assert not offenders, _format(
            offenders,
            "read a token instead (text on a coloured fill is --text-on-fill); "
            "if the colour is new, add it to tokens.css",
        )

    def test_template_styles_have_no_colour_literals(self):
        offenders = []
        for path in _templates():
            text = _blank_comments(path.read_text(), _TEMPLATE_COMMENT)
            for pattern in (_STYLE_ATTR, _STYLE_BLOCK):
                for match in pattern.finditer(text):
                    body = _blank_comments(match.group("body"), _COMMENT)
                    base = _line(text, match.start("body")) - 1
                    for n, decl in _colour_literals_in_css(body, base):
                        offenders.append(f"{path.relative_to(ROOT)}:{n}: {decl}")

        assert not offenders, _format(offenders, "use var(--token) in the template, not a literal colour")

    def test_template_scripts_have_no_colour_literals(self):
        """A script that styles something (a chart, say) reads tokens too; a hex
        fallback beside a token read is a second copy of the palette."""
        offenders = []
        for path in _templates():
            text = _blank_comments(path.read_text(), _TEMPLATE_COMMENT)
            for block in _SCRIPT_BLOCK.finditer(text):
                for match in _JS_COLOUR.finditer(block.group("body")):
                    line = _line(text, block.start("body") + match.start())
                    offenders.append(f"{path.relative_to(ROOT)}:{line}: {match.group(0)}")

        assert not offenders, _format(offenders, "read the colour with getComputedStyle, not a literal")

    def test_the_builder_has_no_colour_literals(self):
        offenders = []
        for path in _builder_sources():
            text = path.read_text()
            for match in _JS_COLOUR.finditer(text):
                offenders.append(f"{path.relative_to(ROOT)}:{_line(text, match.start())}: {match.group(0)}")

        assert not offenders, _format(offenders, "the builder styles through var(--token) like the rest")


class TestEveryTokenIsDefined:
    def test_every_var_reference_is_defined(self):
        sources = [TOKENS, STYLES, *_templates(), *_builder_sources()]
        # Comments quote retired names on purpose ("Studio reads var(--primary-rgb, …)"),
        # so they are blanked before scanning. `//` comments in TS are left alone:
        # the builder has none that name a token.
        texts = {
            path: _blank_comments(_blank_comments(path.read_text(), _COMMENT), _TEMPLATE_COMMENT)
            for path in sources
            if path.exists()
        }

        defined = {name for text in texts.values() for name in _VAR_DEF.findall(text)}
        offenders = []
        for path, text in texts.items():
            for match in _VAR_REF.finditer(text):
                name = match.group(1) or match.group(2)
                if name in defined or name.startswith(FOREIGN_PREFIXES):
                    continue
                offenders.append(f"{path.relative_to(ROOT)}:{_line(text, match.start())}: {name}")

        assert not offenders, _format(
            offenders,
            "an undefined custom property silently drops the whole declaration; "
            "point it at an existing token or define it in tokens.css",
        )


class TestFlowBuilderXyMapping:
    """#flow-builder maps xyflow's whole --xy-* variable surface onto our own
    tokens (Architecture note in styles.css: the builder's stylesheet loads
    after this one, unlayered, and every xyflow rule reads
    var(--xy-thing, var(--xy-thing-default)), so the unsuffixed name set here
    wins by inheritance regardless of source order). Deleting the mapping
    entirely isn't a colour literal (already guarded above) or an undefined
    var() (also already guarded) — it's just the builder quietly reverting to
    xyflow's own light defaults, with nothing else to catch it.
    """

    #: A bare number (line width) and the universal `transparent` keyword —
    #: neither carries a palette value, so neither needs to be var(--token).
    #: Every other --xy-* property here should.
    _NON_COLOUR = frozenset({"--xy-edge-stroke-width", "--xy-attribution-background-color"})

    def test_the_mapping_survives_and_stays_tokenised(self):
        css = _blank_comments(STYLES.read_text(), _COMMENT)
        match = re.search(r"#flow-builder\s*\{([^}]*)\}", css, re.DOTALL)
        assert match, (
            "#flow-builder is gone from styles.css — the builder would fall back to xyflow's own light defaults"
        )
        declarations = [
            (name, value) for name, value in _DECLARATION.findall(match.group(1)) if name.startswith("--xy-")
        ]

        offenders = [
            f"{name}: {value.strip()}"
            for name, value in declarations
            if name not in self._NON_COLOUR and "var(" not in value
        ]
        assert not offenders, _format(offenders, "every --xy-* colour here should read var(--our-token), not a literal")

        # 28 as of Phase 2b; a lower count means a chunk of the mapping got
        # deleted, not that xyflow shrank its own variable surface.
        assert len(declarations) >= 25, (
            f"only {len(declarations)} --xy-* declarations found under #flow-builder — "
            "did part of the mapping get deleted?"
        )


class TestEveryRootCarriesTheTheme:
    def test_every_html_root_calls_theme_attrs(self):
        """``data-theme`` and ``data-color-mode`` are what the tokens resolve against,
        so a root template without them is a page no theme or mode reaches.
        Email is exempt for the same reason as above."""
        roots, offenders = [], []
        for path in _templates():
            text = _blank_comments(path.read_text(), _TEMPLATE_COMMENT)
            for match in re.finditer(r"<html\b[^>]*>", text):
                roots.append(path)
                if "{% theme_attrs %}" not in match.group(0):
                    offenders.append(f"{path.relative_to(ROOT)}:{_line(text, match.start())}: {match.group(0)}")

        assert roots, "found no <html> root at all; the scan is broken"
        assert not offenders, _format(offenders, "add {% theme_attrs %} to the <html> tag (load common_extras)")
