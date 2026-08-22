"""Keyword matching — SPEC §10's three modes, and the one distinction that matters.

``exact`` and ``contains`` are what they sound like. ``any_word`` is the one
worth writing down, because it is the only thing that separates it from
``contains`` and it is the thing people complain about: ``"cat"`` in ``any_word``
matches *"my cat sleeps"* and does **not** match *"category"*, where ``contains``
matches both. A test pins that pair.

Case-insensitive and trimmed on both sides, per SPEC. ``casefold()`` rather than
``lower()``: "case-insensitive" is a SPEC word and ``lower()`` gets German ß and
Turkish İ wrong, which is not hypothetical for a product whose contacts are
strangers on the internet.
"""

import re
from collections.abc import Iterable
from typing import Any

__all__ = ["MAX_MATCH_CHARS", "matches_any", "normalise"]

#: How much of a message is scanned.
#:
#: ``apps.messaging.ingest`` stores up to ``MAX_TEXT_CHARS`` (100k) because a
#: bounded row is the point there. Scanning all of it against a hundred keywords
#: once per inbound event is a different question, and SPEC §7.1 budgets the
#: whole reply at 1.5 seconds. Nothing legitimate hides a keyword 8k characters
#: into a chat message (SECURITY-BASELINE §7).
MAX_MATCH_CHARS = 8192

#: ``\w+`` under Unicode, so "café" and "naïve" are single words.
_WORD_RE = re.compile(r"\w+", re.UNICODE)


def normalise(text: Any) -> str:
    """Trimmed, casefolded and capped — the form both sides of a match are in."""
    if not isinstance(text, str):
        return ""
    return text.strip()[:MAX_MATCH_CHARS].casefold()


def matches_any(text: str, keywords: Iterable[Any]) -> bool:
    """Whether ``text`` matches any of these ``{"text", "mode"}`` entries.

    The word set is built lazily and at most once, however many ``any_word``
    keywords there are — a hundred keywords over one message should tokenise the
    message once, not a hundred times.
    """
    haystack = normalise(text)
    if not haystack:
        return False

    words: set[str] | None = None
    for keyword in keywords:
        if not isinstance(keyword, dict):
            continue
        needle = normalise(keyword.get("text"))
        if not needle:
            continue
        mode = keyword.get("mode")
        if mode == "exact":
            if haystack == needle:
                return True
        elif mode == "contains":
            if needle in haystack:
                return True
        elif mode == "any_word":
            if words is None:
                words = set(_WORD_RE.findall(haystack))
            if needle in words:
                return True
    return False
