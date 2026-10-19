"""Derive a dark palette from the light one, mathematically.

Phase 2c (docs/themes-roadmap.md): the user's own decision is OKLCH
inversion plus an AA contrast solve against the same pairs the Phase 2a
guard checks — no hand-picked dark colours. Keeping hue and chroma and
inverting lightness is what lets a warm, orange brand and warm neutrals
come back out the other side still warm and still orange, rather than a
generic desaturated "dark mode" skin.

Python's stdlib ``colorsys`` has HSV/HLS/YIQ, nothing perceptually uniform,
so OKLab/OKLCH is implemented directly below from Björn Ottosson's published
matrices (https://bottosson.github.io/posts/oklab/) rather than reaching for
a colour-maths dependency for one conversion.

Run it with::

    .venv/bin/python scripts/derive_dark_palette.py

It reads every hex-literal custom property in tokens.css's ``:root`` block,
skips the ones that get no dark counterpart at all (``--platform-*`` brand
marks, ``--text-on-fill``, which is documented to stay white in every mode),
and writes theme/static_src/src/themes/brightbean.css — overwriting whatever
is there, since this is generated output, not hand-maintained.

tests/test_theme_tokens.py's TestFlowBuilderXyMapping and
tests/test_theme_contrast.py prove the result: the latter starts actually
exercising its dark branch once this file has real light-dark() values.
"""

from __future__ import annotations

import math
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.development")

import django  # noqa: E402

django.setup()

from tests.test_theme_contrast import _CONTENT_SURFACES, PAIRS, _contrast_ratio, _parse_tokens  # noqa: E402

THEME_CSS = ROOT / "theme" / "static_src" / "src" / "themes" / "brightbean.css"

# ---------------------------------------------------------------------------
# sRGB <-> OKLCH
# ---------------------------------------------------------------------------


def _srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _linear_to_srgb(c: float) -> float:
    c = max(0.0, min(1.0, c))
    return c * 12.92 if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


def hex_to_oklch(hex_colour: str) -> tuple[float, float, float]:
    """``#RRGGBB`` -> ``(L, C, H)``, H in degrees."""
    h = hex_colour.lstrip("#")
    r, g, b = (_srgb_to_linear(int(h[i : i + 2], 16) / 255) for i in (0, 2, 4))

    l_ = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
    m_ = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
    s_ = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b
    l_, m_, s_ = l_ ** (1 / 3), m_ ** (1 / 3), s_ ** (1 / 3)

    lab_l = 0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_
    lab_a = 1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_
    lab_b = 0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_

    c = math.hypot(lab_a, lab_b)
    h_deg = math.degrees(math.atan2(lab_b, lab_a)) % 360
    return lab_l, c, h_deg


def _oklch_to_linear_rgb(lightness: float, chroma: float, hue_deg: float) -> tuple[float, float, float]:
    h_rad = math.radians(hue_deg)
    lab_a, lab_b = chroma * math.cos(h_rad), chroma * math.sin(h_rad)

    l_ = lightness + 0.3963377774 * lab_a + 0.2158037573 * lab_b
    m_ = lightness - 0.1055613458 * lab_a - 0.0638541728 * lab_b
    s_ = lightness - 0.0894841775 * lab_a - 1.2914855480 * lab_b
    l_, m_, s_ = l_**3, m_**3, s_**3

    r = 4.0767416621 * l_ - 3.3077115913 * m_ + 0.2309699292 * s_
    g = -1.2684380046 * l_ + 2.6097574011 * m_ - 0.3413193965 * s_
    b = -0.0041960863 * l_ - 0.7034186147 * m_ + 1.7076147010 * s_
    return r, g, b


def _in_gamut(rgb: tuple[float, float, float], epsilon: float = 1e-4) -> bool:
    return all(-epsilon <= channel <= 1 + epsilon for channel in rgb)


def oklch_to_hex(lightness: float, chroma: float, hue_deg: float) -> str:
    """``(L, C, H)`` -> ``#RRGGBB``.

    A chroma that's perfectly valid at one lightness can be well outside the
    sRGB gamut at another — the gamut's chroma ceiling shrinks sharply near
    L=0 and L=1 — and clamping each RGB channel independently throws hue
    away exactly where that happens: a handful of distinct pastel tints all
    inverted down near L=0 each went barely negative on different channels
    and every one of them clamped straight to #000000, losing which hue they
    even were. CSS Color 4's own gamut-mapping algorithm fixes this by
    holding L and H fixed and bisecting *chroma* down until the colour is
    back in gamut, instead of clamping each channel after the fact — same
    thing here, just stopping at "in gamut" rather than also doing the
    spec's final cusp adjustment, which isn't worth the extra complexity for
    a palette this small.
    """
    rgb = _oklch_to_linear_rgb(lightness, chroma, hue_deg)
    if not _in_gamut(rgb):
        lo, hi = 0.0, chroma
        for _ in range(24):
            mid = (lo + hi) / 2
            rgb_mid = _oklch_to_linear_rgb(lightness, mid, hue_deg)
            lo, hi = (mid, hi) if _in_gamut(rgb_mid) else (lo, mid)
        rgb = _oklch_to_linear_rgb(lightness, lo, hue_deg)

    r, g, b = (max(0, min(255, round(_linear_to_srgb(x) * 255))) for x in rgb)
    return f"#{r:02X}{g:02X}{b:02X}"


# ---------------------------------------------------------------------------
# Which tokens get a dark counterpart, and how
# ---------------------------------------------------------------------------

#: Exempt entirely — the roadmap's own explicit calls. Brand marks are
#: someone else's colour, not ours to invert; --text-on-fill sits on a
#: saturated fill that already supplies its own contrast regardless of mode
#: (that's the whole point of the token, per tokens.css's own comment).
#:
#: --neutral-950 and --scrim are exempt for a different reason: they're not
#: lightness-coded *content*, they're a function — a shadow's dark cast
#: (--shadow-color: var(--neutral-950)) and a dimming overlay
#: (.sidebar-backdrop's 30% scrim). Inverting them flipped a shadow into a
#: pale glow and a "dim the page behind this modal" overlay into one that
#: brightens it — the opposite of what each is for in every mode (found by
#: ai-zirek[bot], PR #12 review). Leaving them out of the generated file
#: means tokens.css's own plain literal keeps applying unchanged, same as
#: --text-on-fill already does.
_EXEMPT_PREFIXES = ("--platform-",)
_EXEMPT_NAMES = frozenset({"--text-on-fill", "--neutral-950", "--scrim"})

#: "A small lift so the darkest surface isn't pure black" (roadmap, Phase 2
#: Architecture note) — applied to *every* token's inversion, not just the
#: neutral family. First run without this only floored neutrals, on the
#: theory that only "surfaces" needed it; several soft tints (--brand-50,
#: --warning-50, every --accent-*-soft) are pale enough that L' = 1 - L lands
#: near 0, where the sRGB gamut is so narrow that even their small original
#: chroma pushed one or two channels slightly negative — and every one of
#: them clamped straight to the same #000000, hue and all. A starting point,
#: not a final call either way: the user reviews actual screenshots in
#: Phase 2d.
FLOOR_L = 0.15

#: WCAG 2.x AA, matching tests/test_theme_contrast.py's own _MIN_RATIO.
MIN_CONTRAST = {"text": 4.5, "non-text": 3.0}

#: --surface-0/--surface-page/--surface-1(=neutral-50)/--surface-2
#: (=neutral-100) — test_theme_contrast._CONTENT_SURFACES. Deliberately
#: near-identical in light mode (surface-1/2 are a few percent duller than
#: surface-0, just enough to read as a sunken inset). Flooring each one
#: independently collapsed all four to the exact same dark hex: their
#: original chroma is tiny (they're all near-white), so once each hit
#: FLOOR_L on its own, the residual hue difference rounds away to nothing
#: (ai-zirek[bot], PR #12 review). Keeping their light-mode rank — surface-0
#: stays the brightest of the four, surface-2 the dullest — and staggering
#: by a fixed step off the floor is what a hand-drawn 4-step elevation ramp
#: would do; that's what independent per-token flooring can't give it.
_SURFACE_ELEVATION_STEP = 0.02

_HEX_LITERAL = re.compile(r"^#[0-9a-fA-F]{6}$")
_VAR_CALL = re.compile(r"var\(\s*(--[\w-]+)\s*\)")


def _trace_to_literal(name: str, raw_tokens: dict[str, str]) -> str:
    """Follow ``var()`` chains to the token name actually holding a value —
    e.g. ``--text-tertiary`` -> ``--neutral-500``. A no-op for a name that
    already holds its own literal."""
    ref = _VAR_CALL.fullmatch(raw_tokens[name].strip())
    return _trace_to_literal(ref.group(1), raw_tokens) if ref else name


def _naive_dark(hex_colour: str) -> tuple[float, float, float]:
    """Plain ``L' = max(1 - L, FLOOR_L)`` inversion, same hue and chroma."""
    lightness, c, h = hex_to_oklch(hex_colour)
    return max(1 - lightness, FLOOR_L), c, h


def _solve_lightness(l_start: float, c: float, h: float, bg_hexes: list[str], min_contrast: float) -> float:
    """Bisect ``L`` toward white until the colour clears ``min_contrast`` against
    every one of ``bg_hexes`` — the concrete shape of "lightness is solved
    against the same pairs 2a's guard checks." Only ever moves lighter: these
    are dark-mode foreground tokens sitting on dark-mode surfaces, so the fix
    for "not enough contrast" is "lighter," never "darker.\""""

    def worst_ratio(lightness: float) -> float:
        fg_hex = oklch_to_hex(lightness, c, h)
        return min(_contrast_ratio(fg_hex, bg) for bg in bg_hexes)

    if worst_ratio(l_start) >= min_contrast:
        return l_start
    if worst_ratio(1.0) < min_contrast:
        raise ValueError(
            f"can't reach {min_contrast}:1 even at L=1 ({oklch_to_hex(1.0, c, h)}) "
            f"against {bg_hexes} — the floor or a background needs a human look, not more bisecting"
        )
    lo, hi = l_start, 1.0
    for _ in range(40):
        mid = (lo + hi) / 2
        if worst_ratio(mid) >= min_contrast:
            hi = mid
        else:
            lo = mid
    return hi


def _stagger_surface_elevation(
    literal_names: list[str],
    oklch_by_name: dict[str, tuple[float, float, float]],
    dark_hex_by_name: dict[str, str],
) -> None:
    """Give each surface-elevation literal its own dark ``L``, spaced by
    ``_SURFACE_ELEVATION_STEP``, in the same brightest-to-dullest order they
    have in light mode — instead of each independently collapsing to the
    same FLOOR_L."""
    ordered = sorted(literal_names, key=lambda n: oklch_by_name[n][0], reverse=True)
    for rank, name in enumerate(ordered):
        _, c, h = oklch_by_name[name]
        step_l = FLOOR_L + (len(ordered) - 1 - rank) * _SURFACE_ELEVATION_STEP
        dark_hex_by_name[name] = oklch_to_hex(step_l, c, h)


def derive() -> dict[str, tuple[str, str]]:
    """``{token: (light_hex, dark_hex)}`` for every token Phase 2c touches."""
    raw_tokens = _parse_tokens()
    oklch_by_name: dict[str, tuple[float, float, float]] = {}
    dark_hex_by_name: dict[str, str] = {}
    light_hex_by_name: dict[str, str] = {}

    for name, raw_value in raw_tokens.items():
        value = raw_value.strip().rstrip(";")
        if name in _EXEMPT_NAMES or name.startswith(_EXEMPT_PREFIXES) or not _HEX_LITERAL.match(value):
            continue
        light_hex_by_name[name] = value.upper()
        oklch_by_name[name] = hex_to_oklch(value)
        l_dark, c, h = _naive_dark(value)
        dark_hex_by_name[name] = oklch_to_hex(l_dark, c, h)

    # Surface-elevation tokens get their own distinct dark steps before the
    # solve pass below, since that pass reads *their* dark values as the
    # backgrounds everything else is checked against.
    surface_literals = [
        literal
        for literal in (_trace_to_literal(name, raw_tokens) for name in _CONTENT_SURFACES)
        if literal in oklch_by_name
    ]
    _stagger_surface_elevation(surface_literals, oklch_by_name, dark_hex_by_name)

    # Solve pass: every PAIRS row's foreground, traced back to the literal
    # token actually holding a value, gets re-derived against the (now-known)
    # dark backgrounds it has to sit on in PAIRS, at its role's threshold —
    # instead of whatever bare inversion happened to produce.
    backgrounds_by_literal: dict[str, tuple[str, list[str]]] = {}
    for fg, bg, role in PAIRS:
        fg_literal = _trace_to_literal(fg, raw_tokens)
        bg_literal = _trace_to_literal(bg, raw_tokens)
        if fg_literal in oklch_by_name and bg_literal in dark_hex_by_name:
            entry = backgrounds_by_literal.setdefault(fg_literal, (role, []))
            if entry[0] != role:
                raise ValueError(f"{fg_literal} appears in PAIRS under two different roles")
            entry[1].append(dark_hex_by_name[bg_literal])

    for name, (role, bg_hexes) in backgrounds_by_literal.items():
        lightness, c, h = oklch_by_name[name]
        solved_l = _solve_lightness(max(1 - lightness, FLOOR_L), c, h, bg_hexes, MIN_CONTRAST[role])
        dark_hex_by_name[name] = oklch_to_hex(solved_l, c, h)

    return {name: (light_hex_by_name[name], dark_hex_by_name[name]) for name in light_hex_by_name}


def render(values: dict[str, tuple[str, str]]) -> str:
    lines = [
        "/* Generated by scripts/derive_dark_palette.py — do not hand-edit.",
        " * Regenerate after any Layer-1 colour change in tokens.css. */",
        "",
        '[data-theme="brightbean"] {',
    ]
    for name, (light, dark) in values.items():
        lines.append(f"  {name}: light-dark({light}, {dark});")
    lines.append("}")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    values = derive()
    THEME_CSS.parent.mkdir(parents=True, exist_ok=True)
    THEME_CSS.write_text(render(values))
    print(f"wrote {len(values)} token(s) to {THEME_CSS.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
