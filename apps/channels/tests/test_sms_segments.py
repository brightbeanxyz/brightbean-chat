"""Segment arithmetic, against Twilio's own numbers (SPEC §6.6, issue #20).

A table, because :mod:`apps.channels.segments` is pure — no Django, no database,
no clock — and the acceptance criterion is arithmetic: "segment counter matches
Twilio's arithmetic (GSM-7, UCS-2, concatenated)".

The boundaries are what matter. Anyone can count 160 characters; the bugs live
at 161 (which costs **two** segments of 153, not 160 plus one), at the extension
characters that cost two septets each, and at the emoji that is two UTF-16 code
units and must not be split across a boundary.
"""

import pytest

from apps.channels.segments import (
    GSM7_CONCATENATED,
    GSM7_SINGLE,
    UCS2_CONCATENATED,
    UCS2_SINGLE,
    Encoding,
    segments_for,
)

# A character in the GSM-7 extension table: one character, two septets.
EURO = "€"
# Outside the BMP: one character, two UTF-16 code units.
EMOJI = "😀"


class TestEncoding:
    @pytest.mark.parametrize(
        "text",
        [
            "",
            "Plain ASCII, with punctuation!",
            # Every one of these is in the GSM-7 basic alphabet, which is the
            # part people get wrong: an accented French message is *not*
            # automatically UCS-2.
            "Café à Ø Æ ñ ü § ¿ £ ¥ Δ Φ Ω",
            f"Prices in {EURO}",
            "Braces {like} these",
        ],
    )
    def test_gsm7_alphabet(self, text: str) -> None:
        assert segments_for(text).encoding == Encoding.GSM7

    @pytest.mark.parametrize(
        "text",
        [
            # The classic: a curly quote pasted from a word processor.
            "It’s here",
            "An em dash — like this",
            f"Emoji {EMOJI}",
            "日本語",
        ],
    )
    def test_anything_else_is_ucs2(self, text: str) -> None:
        assert segments_for(text).encoding == Encoding.UCS2

    def test_one_stray_character_re_encodes_the_whole_message(self) -> None:
        """The surprise this preview exists to remove."""
        plain = segments_for("a" * 100)
        with_curly = segments_for("a" * 99 + "’")

        assert plain.segments == 1
        assert with_curly.encoding == Encoding.UCS2
        assert with_curly.segments == 2


class TestGsm7Boundaries:
    @pytest.mark.parametrize(
        ("length", "expected"),
        [
            (0, 1),
            (1, 1),
            (GSM7_SINGLE, 1),
            # One over the single-segment limit costs two *concatenated*
            # segments, because both parts now carry a 7-septet header.
            (GSM7_SINGLE + 1, 2),
            (GSM7_CONCATENATED * 2, 2),
            (GSM7_CONCATENATED * 2 + 1, 3),
            (GSM7_CONCATENATED * 3, 3),
        ],
    )
    def test_segment_count(self, length: int, expected: int) -> None:
        assert segments_for("a" * length).segments == expected

    def test_an_extension_character_costs_two_septets(self) -> None:
        count = segments_for(EURO * 80)

        assert count.characters == 80
        assert count.units == 160
        assert count.segments == 1

    def test_an_escape_pair_is_not_split_across_a_boundary(self) -> None:
        """The case that separates packing from dividing.

        153 is odd and a euro costs two septets, so a part holds 76 of them and
        wastes the 153rd — the ESC and its character may not straddle a
        boundary. 153 euros is therefore **three** parts (76, 76, 1), while
        dividing the 306 septets by 153 says two. Picked deliberately: at almost
        every other length the two agree, which is why a wrong implementation
        survives casual testing.
        """
        count = segments_for(EURO * 153)

        assert count.units == 306
        assert -(-count.units // GSM7_CONCATENATED) == 2, "the naive answer, for contrast"
        assert count.segments == 3


class TestUcs2Boundaries:
    @pytest.mark.parametrize(
        ("length", "expected"),
        [
            (UCS2_SINGLE, 1),
            (UCS2_SINGLE + 1, 2),
            (UCS2_CONCATENATED * 2, 2),
            (UCS2_CONCATENATED * 2 + 1, 3),
        ],
    )
    def test_segment_count(self, length: int, expected: int) -> None:
        assert segments_for("’" * length).segments == expected

    def test_a_surrogate_pair_costs_two_units(self) -> None:
        count = segments_for(EMOJI * 35)

        assert count.characters == 35
        assert count.units == 70
        assert count.segments == 1

    def test_a_surrogate_pair_is_not_split_across_a_boundary(self) -> None:
        """The UCS-2 twin of the euro case above.

        67 is odd and an emoji is two code units, so a part holds 33 and wastes
        the 67th. 67 emoji is three parts; dividing 134 by 67 says two. Splitting
        the pair would also produce two segments neither of which contains a
        character.
        """
        count = segments_for(EMOJI * 67)

        assert count.units == 134
        assert -(-count.units // UCS2_CONCATENATED) == 2, "the naive answer, for contrast"
        assert count.segments == 3


class TestReporting:
    def test_it_reports_the_limit_that_applies_to_this_message(self) -> None:
        assert segments_for("a").limit == GSM7_SINGLE
        assert segments_for("a" * 200).limit == GSM7_CONCATENATED
        assert segments_for("’").limit == UCS2_SINGLE
        assert segments_for("’" * 100).limit == UCS2_CONCATENATED

    def test_remaining_counts_units_not_characters(self) -> None:
        count = segments_for(EURO * 10)

        assert count.characters == 10
        assert count.units == 20
        assert count.remaining == GSM7_SINGLE - 20

    def test_remaining_never_goes_negative(self) -> None:
        assert segments_for("a" * 5000).remaining >= 0
