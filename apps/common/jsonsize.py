"""Measuring a value the way a Postgres ``jsonb`` column will store it.

Three call sites carried their own copy of this one line and had drifted apart
on two keyword arguments, each of which changes the answer:

``ensure_ascii``
    The default escapes every non-ASCII character, so an emoji is counted as
    the twelve bytes of its surrogate-pair escape where the column holds four.
    A cap measured that way refuses a document at a third of its stated limit,
    and names a size the database would never have stored.

``allow_nan``
    The default emits bare ``NaN`` and ``Infinity``, which are a CPython
    extension rather than JSON. ``jsonb`` refuses both, so a document measured
    as storable is one the write then rejects — an authenticated save turned
    into a 500.

A third case belongs with them although neither flag names it: a lone
surrogate, which ``json.loads`` will hand you from an untrusted body, survives
``json.dumps`` and then raises ``UnicodeEncodeError`` from ``.encode("utf-8")``.
``jsonb`` cannot hold one of those either.

All three are the same answer to a caller: this value has no size in a ``jsonb``
column, because it cannot go in one. Hence ``None`` rather than a sentinel
integer — the copies this replaces spelled that case three different ways
(``-1``, ``cap + 1``, and an exception that escaped to the request handler),
and only the first of them was deliberate.
"""

import json
from typing import Any

__all__ = ["jsonb_byte_size"]


def jsonb_byte_size(value: Any) -> int | None:
    """Bytes ``value`` occupies as ``jsonb``, or ``None`` when it cannot be stored.

    Pure, and never raises: every caller is on a write path reached from
    something a stranger sent, where an exception is a 500 rather than a
    finding.
    """
    try:
        # UnicodeEncodeError is a ValueError, so the one clause covers all
        # three refusals: unserializable type, non-finite number, lone
        # surrogate.
        return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8"))
    except (TypeError, ValueError):
        return None
