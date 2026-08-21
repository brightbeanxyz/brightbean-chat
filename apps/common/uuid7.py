"""UUIDv7 generation (RFC 9562 §5.7).

Python 3.12 has no ``uuid.uuid7``; it arrives in 3.14. Rather than take a
dependency for ~40 lines (SECURITY-BASELINE §10 — every dependency is supply
chain), this implements the RFC directly.

Layout, most significant bit first::

    unix_ts_ms   48 bits   big-endian milliseconds since the Unix epoch
    ver           4 bits   0b0111
    rand_a       12 bits   monotonic sub-millisecond counter (RFC 9562 §6.2
                           "Replace Leftmost Random Bits with Increased Clock
                           Precision", method 3: a per-millisecond counter)
    var           2 bits   0b10
    rand_b       62 bits   CSPRNG

Using rand_a as a counter rather than random bits buys strict monotonicity
within a millisecond, which is what makes v7 primary keys index-friendly:
inserts land at the right-hand edge of the B-tree instead of scattering.
"""

import os
import threading
import time
import uuid

__all__ = ["uuid7"]

_MAX_COUNTER = 0xFFF  # 12 bits of rand_a

_lock = threading.Lock()
_last_timestamp_ms = -1
_counter = 0


def _next_timestamp_and_counter() -> tuple[int, int]:
    """Return a (timestamp_ms, counter) pair that never repeats or goes backwards."""
    global _last_timestamp_ms, _counter

    with _lock:
        timestamp_ms = time.time_ns() // 1_000_000
        if timestamp_ms > _last_timestamp_ms:
            _last_timestamp_ms = timestamp_ms
            _counter = 0
        else:
            # Same millisecond, or a clock that stepped backwards (NTP): keep
            # counting from the last timestamp we handed out so ordering holds.
            _counter += 1
            if _counter > _MAX_COUNTER:
                # More than 4096 ids in one millisecond. Borrow from the future
                # rather than emit a duplicate or a non-monotonic value.
                _last_timestamp_ms += 1
                _counter = 0
        return _last_timestamp_ms, _counter


def uuid7() -> uuid.UUID:
    """Return a new UUIDv7 — time-ordered, monotonic, and unique."""
    timestamp_ms, counter = _next_timestamp_and_counter()

    # 62 bits of randomness; the top 2 bits of rand_b's byte are overwritten by
    # the variant below, so draw 8 bytes and mask them off.
    rand_b = int.from_bytes(os.urandom(8), "big") & ((1 << 62) - 1)

    value = (timestamp_ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76  # version
    value |= counter << 64  # rand_a
    value |= 0b10 << 62  # variant
    value |= rand_b

    return uuid.UUID(int=value)
