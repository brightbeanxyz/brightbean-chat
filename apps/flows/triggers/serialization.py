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

import json
import logging
from typing import Any

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.channels.events import EventPayload, EventType, NormalizedEvent

__all__ = [
    "MAX_ROUTE_ATTACHMENTS",
    "MAX_ROUTE_EXTRA_KEYS",
    "MAX_ROUTE_PAYLOAD_BYTES",
    "MAX_ROUTE_TEXT_CHARS",
    "event_to_payload",
    "payload_to_event",
    "shrink_to_fit",
]

logger = logging.getLogger(__name__)

_EVENT_TYPES: frozenset[str] = frozenset(member.value for member in EventType)

#: Matches what a wait will actually read from an answer. Carrying the full
#: 100k ``apps.messaging.ingest`` is willing to *store* would put an unbounded
#: string in a queue row for no reader.
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
    body = document.get("event", {}).get("payload")
    if not isinstance(body, dict):
        return document if _size(document) <= MAX_ROUTE_PAYLOAD_BYTES else None
    if _size(document) <= MAX_ROUTE_PAYLOAD_BYTES:
        return document

    for field, replacement in (("extra", {}), ("attachments", []), ("media_ids", [])):
        body[field] = replacement
        logger.info("Dropped %s from a route_event payload to fit the size cap.", field)
        if _size(document) <= MAX_ROUTE_PAYLOAD_BYTES:
            return document

    body["text"] = body.get("text", "")[:1000]
    if _size(document) <= MAX_ROUTE_PAYLOAD_BYTES:
        logger.info("Truncated the text of a route_event payload to fit the size cap.")
        return document
    return None


def _size(document: dict[str, Any]) -> int:
    return len(json.dumps(document, separators=(",", ":")).encode("utf-8"))


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
