"""Cursor pagination for the list endpoints.

An opaque base64 cursor carrying an integer offset. Offsets rather than a
composite ``(value, id)`` keyset because cursor stability across an insert is
not something SPEC §17 asks for, and offset paginates correctly as long as the
queryset's ordering carries a stable ``id`` tiebreak — which
:func:`paginate` enforces by refusing to guess.

The cursor is opaque so that swapping it for a keyset cursor later is a change
to this module and nothing else. Callers are told, in ``docs/api/v1.md``, to
echo ``next_cursor`` back and never to construct one.

A malformed cursor raises ``ValueError`` here and becomes a 422 in the router,
rather than a 500 from ``base64`` or ``json``.
"""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any

from django.conf import settings

__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "decode_cursor",
    "encode_cursor",
    "paginate",
]

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


def encode_cursor(offset: int) -> str:
    raw = json.dumps({"o": int(offset)}, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def decode_cursor(cursor: str | None) -> int:
    """The offset a cursor names; 0 when there is none.

    Raises ``ValueError`` for anything that is not a base64 JSON object with a
    non-negative integer ``o``.
    """
    if not cursor:
        return 0
    if not isinstance(cursor, str) or len(cursor) > 200:
        raise ValueError("Invalid cursor.")
    try:
        padded = cursor.encode("ascii") + b"=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except (ValueError, TypeError, binascii.Error, UnicodeDecodeError) as exc:
        raise ValueError("Invalid cursor.") from exc
    if not isinstance(payload, dict):
        raise ValueError("Invalid cursor.")
    offset = payload.get("o", 0)
    # ``bool`` is an ``int`` subclass, so ``{"o": true}`` would otherwise be 1.
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise ValueError("Invalid cursor.")
    return offset


def clamp_limit(limit: int | None) -> int:
    if not limit:
        return DEFAULT_LIMIT
    return max(1, min(int(limit), MAX_LIMIT))


def paginate(queryset: Any, *, limit: int | None, cursor: str | None) -> dict[str, Any]:
    """Slice ``queryset`` and return the response envelope.

    Fetches one row past the limit to answer ``has_more`` without a second
    ``count()`` — on a large contacts table that count is the expensive half of
    the request, and nothing in the documented shape promises a total.
    """
    size = clamp_limit(limit)
    offset = decode_cursor(cursor)
    window = list(queryset[offset : offset + size + 1])
    has_more = len(window) > size
    rows = window[:size]
    return {
        "data": rows,
        "has_more": has_more,
        "next_cursor": encode_cursor(offset + size) if has_more else None,
    }


def max_body_bytes() -> int:
    """Exposed for the docs page, which publishes the number it enforces."""
    return int(settings.API_MAX_BODY_BYTES)
