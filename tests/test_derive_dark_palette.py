"""Pure-function guards for scripts/derive_dark_palette.py.

The generated output is proven end to end by test_theme_contrast.py (it
reads the real themes/brightbean.css and checks WCAG AA). These pin the two
smaller behaviours that contrast alone can't catch: a near-white pastel
keeps enough chroma to still look tinted rather than gray once floored, and
a circular var() chain fails loudly instead of recursing forever. Found
missing in a second read of PR #12, after merge — a chroma-only version of
the first guard shipped first and silently tinted the warm neutral ramp
(--neutral-700/-400) brown, since that ramp also clears the chroma bar, just
at a lightness the floor never touches.
"""

import pytest

from scripts.derive_dark_palette import (
    _MIN_TINT_CHROMA,
    FLOOR_L,
    _naive_dark,
    _trace_to_literal,
)


class TestNaiveDarkChromaFloor:
    def test_a_pale_tint_keeps_visible_chroma_at_the_floor(self):
        # --warning-50's real light value: near-white, but intentionally a
        # pale yellow, not a pale gray.
        dark_l, c, _h = _naive_dark("#FEFCE8")
        assert dark_l == FLOOR_L
        assert c >= _MIN_TINT_CHROMA

    def test_a_true_neutral_is_not_tinted(self):
        dark_l, c, _h = _naive_dark("#FFFFFF")
        assert dark_l == FLOOR_L
        assert c == pytest.approx(0.0, abs=1e-6)

    def test_a_near_white_surface_just_under_the_bar_is_not_tinted(self):
        # --surface-page: near-white like the pale tints above (so the floor
        # does fire, unlike --neutral-700), but its own chroma (0.0054) sits
        # just under _NEUTRAL_CHROMA_THRESHOLD rather than at the C=0 extreme
        # #FFFFFF already covers — the gate itself, not just its endpoints.
        dark_l, c, _h = _naive_dark("#F7F6F2")
        assert dark_l == FLOOR_L
        assert c < _MIN_TINT_CHROMA

    def test_a_mid_lightness_warm_neutral_is_not_boosted(self):
        # --neutral-700: warm enough to clear the chroma bar on its own, but
        # never near-white, so the floor never bites and this must invert
        # untouched — the regression a chroma-only gate shipped first.
        dark_l, c, _h = _naive_dark("#44403C")
        assert dark_l != FLOOR_L
        assert c < _MIN_TINT_CHROMA


class TestTraceToLiteralCycleGuard:
    def test_a_circular_var_chain_raises_instead_of_recursing(self):
        tokens = {"--a": "var(--b)", "--b": "var(--a)"}
        with pytest.raises(ValueError, match="circular"):
            _trace_to_literal("--a", tokens)


class TestTraceToLiteralStopsAtAFallback:
    def test_a_fallback_with_an_undefined_primary_name_is_not_chased(self):
        # var(--missing, ...) names a token that was never given its own
        # entry on purpose — that's what the fallback is for. Chasing into
        # --missing anyway would KeyError on the next lookup (found by
        # ai-zirek[bot] reviewing PR #17, from an earlier version of
        # _VAR_CALL that matched the fallback form). Stopping here and
        # returning --has-fallback's own name, same as any other
        # non-bare-var() value, is what keeps this safe.
        tokens = {"--has-fallback": "var(--missing, #FAFAFA)"}
        assert _trace_to_literal("--has-fallback", tokens) == "--has-fallback"
