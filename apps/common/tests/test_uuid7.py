"""UUIDv7 conformance (RFC 9562 §5.7)."""

import itertools
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from apps.common import uuid7 as uuid7_module
from apps.common.uuid7 import uuid7


def _timestamp_ms(value: uuid.UUID) -> int:
    return value.int >> 80


class TestBitLayout:
    def test_version_is_7(self):
        assert uuid7().version == 7

    def test_variant_is_rfc_4122(self):
        # RFC 9562 keeps the two-bit 0b10 variant from RFC 4122.
        assert uuid7().variant == uuid.RFC_4122
        assert (uuid7().int >> 62) & 0b11 == 0b10

    def test_timestamp_matches_wall_clock(self):
        before = time.time_ns() // 1_000_000
        value = uuid7()
        after = time.time_ns() // 1_000_000

        # Allow a millisecond of slack on each side for clock granularity.
        assert before - 1 <= _timestamp_ms(value) <= after + 1

    def test_is_a_real_uuid_instance(self):
        value = uuid7()
        assert isinstance(value, uuid.UUID)
        assert uuid.UUID(str(value)) == value


class TestMonotonicity:
    def test_strictly_increasing_within_one_millisecond(self, monkeypatch):
        """A frozen clock forces every id into the same millisecond."""
        monkeypatch.setattr(uuid7_module.time, "time_ns", lambda: 1_700_000_000_123_000_000)

        values = [uuid7() for _ in range(500)]

        assert all(a < b for a, b in itertools.pairwise(values))
        assert len({_timestamp_ms(v) for v in values}) == 1
        assert len(set(values)) == len(values)

    def test_counter_overflow_borrows_from_the_next_millisecond(self, monkeypatch):
        """More than 4096 ids in one millisecond must still be ordered."""
        monkeypatch.setattr(uuid7_module.time, "time_ns", lambda: 1_700_000_000_456_000_000)

        values = [uuid7() for _ in range(4_200)]

        assert all(a < b for a, b in itertools.pairwise(values))
        assert len(set(values)) == len(values)
        # The overflow advanced the embedded timestamp rather than repeating.
        assert len({_timestamp_ms(v) for v in values}) > 1

    def test_ordering_survives_a_backwards_clock(self, monkeypatch):
        """An NTP step backwards must not produce a smaller id."""
        clock = iter([1_700_000_000_500_000_000, 1_700_000_000_400_000_000])
        monkeypatch.setattr(uuid7_module.time, "time_ns", lambda: next(clock))

        first = uuid7()
        second = uuid7()

        assert second > first

    def test_unique_across_threads(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            values = list(pool.map(lambda _: uuid7(), range(2_000)))

        assert len(set(values)) == len(values)
