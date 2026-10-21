"""The one measurement three caps share, and the three ways it answers "no"."""

import json

from apps.common.jsonsize import jsonb_byte_size

EMOJI = "\U0001f600"
#: What `json.loads` hands you for the escape `"\ud800"` — a well-formed JSON
#: document holding a code point that is not a character. Reachable from any
#: webhook body, which is why it is a case rather than a curiosity.
LONE_SURROGATE = json.loads('"\\ud800"')


class TestTheMeasurement:
    def test_ascii_is_the_length_of_the_compact_document(self):
        assert jsonb_byte_size({"a": "bc"}) == len('{"a":"bc"}')

    def test_an_emoji_counts_its_stored_bytes_not_its_escape(self):
        """Four bytes of UTF-8, not the twelve of a surrogate-pair escape.

        The whole point of the shared helper: three caps measured this three
        different ways, and the two that escaped refused documents at a third
        of their stated limit.
        """
        assert jsonb_byte_size(EMOJI) == len(EMOJI.encode("utf-8")) + 2  # + the quotes
        assert jsonb_byte_size(EMOJI) < len(json.dumps(EMOJI).encode("utf-8"))

    def test_it_agrees_with_what_postgres_would_be_handed(self):
        document = {"text": EMOJI * 10, "n": 1, "flag": True, "none": None}
        assert jsonb_byte_size(document) == len(
            json.dumps(document, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        )


class TestTheRefusals:
    def test_a_non_finite_number_has_no_size(self):
        """`json.dumps` writes bare NaN by default and jsonb rejects it, so a
        document measured as storable was one the write then refused."""
        assert jsonb_byte_size({"x": float("nan")}) is None
        assert jsonb_byte_size({"x": float("inf")}) is None

    def test_a_lone_surrogate_has_no_size_and_does_not_raise(self):
        """Survives `json.dumps` and raises from `.encode("utf-8")`. On the
        inbound path that exception was a 500 on a request from a stranger."""
        assert jsonb_byte_size({"text": LONE_SURROGATE}) is None

    def test_an_unserializable_type_has_no_size(self):
        assert jsonb_byte_size({"when": object()}) is None

    def test_it_never_raises_whatever_it_is_handed(self):
        for value in ({"x": float("nan")}, {"t": LONE_SURROGATE}, object(), {1: {2: {3: object()}}}):
            assert jsonb_byte_size(value) is None or isinstance(jsonb_byte_size(value), int)
