"""A ``NormalizedEvent`` through a jsonb column and back.

The queue payload is a document that will sit in a table, possibly for hours,
and every string in it came from a stranger. Two rules follow.

**``raw`` is dropped.** It is unbounded, wholly attacker-controlled, and read by
no routing stage. It is also already stored verbatim in ``webhook_event_log.raw``
(SPEC §5), so nothing is lost for debugging. That is the cheapest possible way to
satisfy SECURITY-BASELINE §7 on a column with no size limit of its own.

**Everything else is re-validated on the way back in**, per field, per type. A
payload that has been sitting in a table is not more trustworthy than the request
that created it — ``apps/flows/handlers.py`` already treats ids that way, and
this treats values the same.
"""

import logging
import unicodedata
from typing import Any

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.channels.events import EventPayload, EventType, NormalizedEvent
from apps.common.jsonsize import jsonb_byte_size

__all__ = [
    "MAX_ROUTE_ATTACHMENTS",
    "MAX_ROUTE_EXTRA_KEYS",
    "MAX_ROUTE_PAYLOAD_BYTES",
    "MAX_ROUTE_SHRUNK_TEXT_BYTES",
    "MAX_ROUTE_TEXT_CHARS",
    "event_to_payload",
    "payload_to_event",
    "shrink_to_fit",
]

#: Zero-width joiner. Chains the parts of a family or a profession emoji,
#: so a cut on either side of one leaves both halves rendering separately.
_ZWJ = "\u200d"

logger = logging.getLogger(__name__)

_EVENT_TYPES: frozenset[str] = frozenset(member.value for member in EventType)

#: Matches what a wait will actually read from an answer. Carrying the full
#: 100k ``apps.messaging.ingest`` is willing to *store* would put an unbounded
#: string in a queue row for no reader.
#:
#: In **characters**, deliberately, because that is the unit the engine reads
#: in — but it is therefore not a bound on bytes, and the document cap below
#: is: 4096 emoji is 16 KiB of UTF-8 on its own. Nothing here reconciles the
#: two units, and nothing needs to; ``shrink_to_fit`` is what enforces the
#: byte budget, and ``MAX_ROUTE_SHRUNK_TEXT_BYTES`` is the rung that does it.
MAX_ROUTE_TEXT_CHARS = 4096
MAX_ROUTE_ATTACHMENTS = 20
MAX_ROUTE_URL_CHARS = 2000
MAX_ROUTE_ID_CHARS = 200
MAX_ROUTE_EXTRA_KEYS = 20
MAX_ROUTE_EXTRA_KEY_CHARS = 64
MAX_ROUTE_EXTRA_VALUE_CHARS = 500
#: The whole payload. A document over this is shrunk in a documented order and,
#: if that is not enough, refused rather than written.
MAX_ROUTE_PAYLOAD_BYTES = 16 * 1024

#: What the last shrink rung leaves of the text. Derived from the payload
#: budget rather than picked, and counted in **bytes** like the budget it
#: serves — the two caps above it are in characters, and the gap between the
#: units is the whole problem: ``MAX_ROUTE_TEXT_CHARS`` of emoji is 16 KiB of
#: UTF-8, the entire document budget, before an id or a timestamp is added.
#: The literal 1000 this replaces happened to be safe only because 1000 emoji
#: is 4000 bytes; nothing said so, and nothing would have caught it moving.
MAX_ROUTE_SHRUNK_TEXT_BYTES = MAX_ROUTE_PAYLOAD_BYTES // 4


def event_to_payload(event: NormalizedEvent) -> dict[str, Any]:
    """The queue form of an event. Bounded, primitive, and without ``raw``."""
    payload = event.payload
    return {
        "type": str(event.type),
        # Bounded by hashing, never by slicing. Persistence stored this
        # platform_user_id through ``bounded_address``, so an over-long one is
        # already a digest in the identity table — truncating here would send the
        # worker looking for a row that does not exist, and it would route the
        # event with no contact. The same rule protects provider_event_id, which
        # keys the RoutedEvent exactly-once guard.
        "platform_user_id": _identifier(event.platform_user_id),
        "provider_event_id": _identifier(event.provider_event_id),
        "timestamp": event.timestamp.isoformat() if event.timestamp else "",
        "payload": {
            "text": _text(payload.text, MAX_ROUTE_TEXT_CHARS),
            "attachments": _texts(payload.attachments, MAX_ROUTE_ATTACHMENTS, MAX_ROUTE_URL_CHARS),
            "button_id": _text(payload.button_id, MAX_ROUTE_ID_CHARS),
            "comment_id": _identifier(payload.comment_id),
            "media_ids": _texts(payload.media_ids, MAX_ROUTE_ATTACHMENTS, MAX_ROUTE_ID_CHARS),
            "ref": _text(payload.ref, MAX_ROUTE_ID_CHARS),
            # Kept, unlike raw, because L4-A ships the platform-agnostic comment
            # infrastructure and the post id a comment trigger scopes on travels
            # here (see apps.flows.triggers.types). Scalars only.
            "extra": _extra(payload.extra),
        },
    }


def payload_to_event(raw: Any, connection: Any) -> NormalizedEvent | None:
    """Rebuild an event, or ``None`` when the document is not one.

    ``connection`` is resolved by the caller from ``action.workspace_id`` and is
    never taken from the payload — the tenancy rule ``apps/flows/handlers.py``
    already applies to every id it reads.
    """
    if not isinstance(raw, dict):
        return None
    event_type = raw.get("type")
    # EventType is a StrEnum, not TextChoices — membership is over its members.
    if event_type not in _EVENT_TYPES:
        logger.warning("A route_event payload named %r, which is not an event type.", str(event_type)[:40])
        return None

    body = raw.get("payload")
    body = body if isinstance(body, dict) else {}
    timestamp = parse_datetime(raw["timestamp"]) if isinstance(raw.get("timestamp"), str) else None

    return NormalizedEvent(
        type=EventType(event_type),
        connection=connection,
        platform_user_id=_identifier(raw.get("platform_user_id")),
        provider_event_id=_identifier(raw.get("provider_event_id")),
        timestamp=timestamp or timezone.now(),
        payload=EventPayload(
            text=_text(body.get("text"), MAX_ROUTE_TEXT_CHARS),
            attachments=_texts(body.get("attachments"), MAX_ROUTE_ATTACHMENTS, MAX_ROUTE_URL_CHARS),
            button_id=_text(body.get("button_id"), MAX_ROUTE_ID_CHARS),
            # A comment id keys the HandledComment guard, so it is bounded the
            # same way rather than sliced.
            comment_id=_identifier(body.get("comment_id")),
            media_ids=_texts(body.get("media_ids"), MAX_ROUTE_ATTACHMENTS, MAX_ROUTE_ID_CHARS),
            ref=_text(body.get("ref"), MAX_ROUTE_ID_CHARS),
            extra=_extra(body.get("extra")),
        ),
    )


def shrink_to_fit(document: dict[str, Any]) -> dict[str, Any] | None:
    """Bring an over-large queue document under the cap, or answer ``None``.

    ``document`` is the whole action payload — ``stage``, ``connection_id`` and
    the serialized ``event`` — because the cap is on what goes in the column.

    The order is deliberate and each step is logged. ``extra`` goes first: only
    a comment trigger reads it, and only a handful of small keys. ``attachments``
    next: no routing stage reads one at all. The text goes last and is truncated
    rather than dropped, because matching *does* read it — and truncating is
    itself a compromise, so a document still too large after all three is
    refused rather than silently turned into something that would match a
    different keyword than the one the contact sent.
    """
    size = _size(document)
    if size is None:
        # Not a size problem: a non-finite number or a lone surrogate is a
        # value `jsonb` refuses at any length, and no rung below can shrink it
        # into one the column accepts.
        logger.warning("A route_event payload holds a value that cannot be stored as JSON; dropping it.")
        return None
    if size <= MAX_ROUTE_PAYLOAD_BYTES:
        return document

    body = document.get("event", {}).get("payload")
    if not isinstance(body, dict):
        return None

    # Copy before the first mutation. The rungs below rewrite the payload in
    # place, and `body` aliases the caller's own dict: a document this function
    # ends up refusing used to go back to the caller already stripped of its
    # extra, attachments and media_ids.
    body = dict(body)
    document = {**document, "event": {**document["event"], "payload": body}}

    for field, replacement in (("extra", {}), ("attachments", []), ("media_ids", [])):
        if not body.get(field):
            # Nothing to drop. Skipping spares a full re-serialisation of a
            # document that has not changed, and spares the log a line saying
            # we dropped something that was never there.
            continue
        body[field] = replacement
        logger.info("Dropped %s from a route_event payload to fit the size cap.", field)
        if _within_cap(document):
            return document

    text = body.get("text", "")
    truncated = _truncate_to_bytes(text, MAX_ROUTE_SHRUNK_TEXT_BYTES) if isinstance(text, str) else ""
    if truncated != text:
        body["text"] = truncated
        if _within_cap(document):
            logger.info("Truncated the text of a route_event payload to fit the size cap.")
            return document
    return None


def _size(document: dict[str, Any]) -> int | None:
    """Bytes the queue's jsonb column will hold, or ``None`` if it will not.

    The measurement is :func:`apps.common.jsonsize.jsonb_byte_size`, shared
    with the flow-graph and trigger-config caps. Two properties of it matter
    here and neither was true of the hand-rolled ``json.dumps`` this replaces:

    It counts UTF-8, not escapes. Over-counting an emoji at three times its
    stored size sent :func:`shrink_to_fit` down the whole ladder on payloads
    that already fit — dropping ``extra``, then the attachments, then
    truncating the very text the keyword matcher reads.

    It answers ``None`` instead of raising. A lone surrogate, which
    ``json.loads`` will hand us straight from a webhook body, survives
    ``json.dumps`` and then raises ``UnicodeEncodeError`` from the encode. On
    this path that exception is not a finding, it is a 500 on an inbound
    request from a stranger.
    """
    return jsonb_byte_size(document)


def _within_cap(document: dict[str, Any]) -> bool:
    """Whether the document now fits. ``None`` — unstorable — is not fitting."""
    size = _size(document)
    return size is not None and size <= MAX_ROUTE_PAYLOAD_BYTES


def _truncate_to_bytes(text: str, limit: int) -> str:
    """At most ``limit`` UTF-8 bytes, cut on a grapheme boundary.

    Two cuts, because the budget and the text are in different units. The byte
    slice lands anywhere, so ``errors="ignore"`` drops the partial code point
    it leaves and gives back a character count; :func:`_cut_at_grapheme` then
    walks that back off a cluster boundary.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return _cut_at_grapheme(text, len(encoded[:limit].decode("utf-8", errors="ignore")))


def _cut_at_grapheme(text: str, limit: int) -> str:
    """``text`` cut at or before ``limit`` characters, never inside a cluster.

    Python strings are code points, so a slice cannot halve an emoji — but it
    can land between the two halves of a flag, between a face and its skin
    tone, or inside the ZWJ chain of a family, and each of those renders as
    the pieces rather than the glyph. A full grapheme segmenter is a
    dependency this repo does not carry; the handful of joiners below are the
    ones reachable from an emoji keyboard.
    """
    if len(text) <= limit:
        return text
    cut = limit
    while cut > 0 and _splits_a_cluster(text, cut):
        cut -= 1
    return text[:cut]


def _splits_a_cluster(text: str, cut: int) -> bool:
    if cut <= 0 or cut >= len(text):
        return False
    if _joins_backwards(text[cut]):
        return True
    if text[cut - 1] == _ZWJ:
        return True
    if _is_regional(text[cut]) and _is_regional(text[cut - 1]):
        # A flag is a *pair* of regional indicators, so whether this cut is
        # inside one depends on how many come before it.
        run = 0
        index = cut - 1
        while index >= 0 and _is_regional(text[index]):
            run += 1
            index -= 1
        return run % 2 == 1
    return False


def _joins_backwards(char: str) -> bool:
    """Whether ``char`` binds to the character before it."""
    code = ord(char)
    return (
        unicodedata.combining(char) != 0
        or char == _ZWJ
        or 0xFE00 <= code <= 0xFE0F  # variation selectors
        or 0xE0100 <= code <= 0xE01EF  # variation selectors supplement
        or 0x1F3FB <= code <= 0x1F3FF  # skin-tone modifiers
        or code == 0x20E3  # combining enclosing keycap
        or 0xE0020 <= code <= 0xE007F  # tag characters, as in subdivision flags
    )


def _is_regional(char: str) -> bool:
    return 0x1F1E6 <= ord(char) <= 0x1F1FF


def _identifier(value: Any) -> str:
    """Bound an id the way the identity table does — see the facade's docstring."""
    from apps.flows import messaging as messaging_facade

    return messaging_facade.bounded_identifier(value, limit=MAX_ROUTE_ID_CHARS)


def _text(value: Any, limit: int) -> str:
    return value[:limit] if isinstance(value, str) else ""


def _texts(values: Any, count: int, limit: int) -> tuple[str, ...]:
    if not isinstance(values, list | tuple):
        return ()
    return tuple(value[:limit] for value in values[:count] if isinstance(value, str))


def _extra(value: Any) -> dict[str, Any]:
    """Scalars only, bounded in count, key length and value length."""
    if not isinstance(value, dict):
        return {}
    kept: dict[str, Any] = {}
    for key, item in value.items():
        if len(kept) >= MAX_ROUTE_EXTRA_KEYS:
            break
        if not isinstance(key, str):
            continue
        if isinstance(item, str):
            kept[key[:MAX_ROUTE_EXTRA_KEY_CHARS]] = item[:MAX_ROUTE_EXTRA_VALUE_CHARS]
        elif isinstance(item, bool | int | float):
            kept[key[:MAX_ROUTE_EXTRA_KEY_CHARS]] = item
    return kept
