"""Instagram adapter — Instagram API with Instagram Login (SPEC §6.3).

The second real adapter, and written from ``providers/telegram.py``, whose
docstring says a Layer-5 author "should be able to replace the helpers and keep
the class". That is what happened here: the class below implements SPEC §6.1's
interface and nothing else, while everything that is *about Instagram* — the
entry/change shapes, the Send API bodies, the 1000-**byte** text cap, the
generic template — lives in the small helpers above it, each named for the thing
it converts.

Inherited and deliberately not re-implemented:

* HTTP mechanics, the timeout policy, ``429`` → :class:`RateLimitError` and the
  "never put a URL in an error message" rule — :mod:`apps.channels.providers.base`;
* block downgrading — :func:`apps.channels.downgrade.downgrade`, shared;
* the raw-body ``X-Hub-Signature-256`` check and the entry/change walk shared
  with Messenger — :mod:`apps.channels.providers.meta_common`;
* the OAuth exchange, token storage and refresh — :mod:`apps.channels.instagram_oauth`,
  out of this module on purpose (the layer brief asks for it, and it is the same
  separation ``views_telegram`` keeps from ``providers.telegram``).

--------------------------------------------------------------------------
Why ``me`` and not the account id
--------------------------------------------------------------------------

Meta documents ``/<IG_ID>/messages`` and ``/me/messages`` as equivalent. This
module uses ``me`` throughout, because the access token *is* the account: an
``IG_ID`` taken from a stored column can disagree with the token after a
reconnect, and the failure mode of that disagreement is a send to the wrong
account rather than an error. The stored ``external_id`` is still load-bearing —
it is how :meth:`InstagramAdapter.resolve_connection` maps an inbound
``entry[].id`` back to a connection — it is simply not used to address outbound
calls.

--------------------------------------------------------------------------
Rate limits, and why there is no throttle in this file
--------------------------------------------------------------------------

Same argument as Telegram's, and it is worth repeating rather than
cross-referencing because it is the first thing an adapter author reaches for.
The global limit is the connection's token bucket (``apps.messaging.buckets``),
configured by ``rate_default=8.0`` in :mod:`apps.channels.policy`. The
per-recipient limit is satisfied by the shape of the system: SPEC §9.6
serialises everything one contact does behind a single advisory lock, so two
messages to one thread cannot be in flight at once. A throttle here would be a
sleep held *inside* that lock. When Meta disagrees anyway it answers ``429`` and
the send pipeline reschedules.

Meta's own published ceiling for private replies is 750 per hour per account,
which is an order of magnitude above what one connection's bucket will pass.

--------------------------------------------------------------------------
Secrets
--------------------------------------------------------------------------

The access token is the account: anyone holding it can read every DM and send as
the account. It lives encrypted in ``connection.credentials`` and appears at
runtime in exactly one place — an ``Authorization: Bearer`` header, deliberately
rather than the ``access_token`` query parameter Meta also accepts, so it never
reaches a proxy access log. Nothing in this module logs a token or a URL;
``base.request_json`` reports the *host* of a failed call and never the path.
"""

import logging
import secrets
import threading
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx
from django.utils import timezone

from apps.channels import ingest as channels_ingest
from apps.channels import security
from apps.channels.capabilities import Capabilities, capabilities_for
from apps.channels.downgrade import downgrade, split_text
from apps.channels.events import (
    Button,
    Card,
    CardBlock,
    EventPayload,
    EventType,
    GalleryBlock,
    MediaBlock,
    NormalizedEvent,
    OutboundMessage,
    QuickReply,
    SendResult,
    SendStatus,
    TextBlock,
)
from apps.channels.instagram_oauth import access_token, mark_needs_reauth
from apps.channels.models import ChannelConnection
from apps.channels.providers import meta_common
from apps.channels.providers.base import BACKGROUND_TIMEOUT, Adapter, request_json
from apps.channels.providers.exceptions import APIError
from apps.channels.registry import register_adapter
from apps.common.platforms import Platform
from apps.flows import messaging as messaging_facade
from apps.flows.triggers.comments import CommentResponder, register_responder
from apps.flows.triggers.types import COMMENT_PARENT_ID_KEY, COMMENT_POST_ID_KEY
from apps.queueing.registry import register_handler

if TYPE_CHECKING:
    from django.http import HttpRequest

logger = logging.getLogger(__name__)

__all__ = [
    "COMMENT_REPLY_ACTION",
    "InstagramAdapter",
    "call",
    "recent_media",
    "wire_messages",
]

#: The Graph host for Instagram Login. A constant rather than a setting: there
#: is one, and a configurable API host for calls carrying an access token is an
#: exfiltration primitive rather than a feature.
API_ROOT = "https://graph.instagram.com"

#: Pinned rather than floating. Meta deprecates a version roughly every two
#: years with a year of notice; a floating "latest" would change payload shapes
#: under a deployment nobody upgraded.
API_VERSION = "v23.0"

#: The ``object`` a genuine Instagram delivery declares. A payload claiming any
#: other object did not come from this platform's subscription, whatever it is
#: signed with.
WEBHOOK_OBJECT = "instagram"

_CAPABILITIES: Capabilities = capabilities_for(Platform.INSTAGRAM)

#: Meta's cap is "1000 **bytes** or less", not characters. The capability table
#: carries 1000 because that is what the flow builder warns authors about, and
#: :func:`_text_messages` enforces the byte reading — a thousand characters of
#: Japanese is three thousand bytes and would be rejected at the platform.
MAX_TEXT_CHARS = _CAPABILITIES.max_text_len
MAX_TEXT_BYTES = 1000

#: How many re-split passes before :func:`_within_bytes` cuts on bytes instead.
MAX_SPLIT_DEPTH = 4

#: Generic template limits, from Meta's reference: ten elements per message,
#: three buttons per element, eighty characters of title and of subtitle.
MAX_TEMPLATE_ELEMENTS = 10
MAX_TEMPLATE_BUTTONS = 3
MAX_TITLE_CHARS = 80

#: A quick reply's label is truncated by Instagram at twenty characters, so it is
#: truncated here instead — a label the platform cuts mid-word is worse than one
#: we cut knowingly, and the downgrade renderer has already capped the *count*.
MAX_LABEL_CHARS = 20

#: Postback and quick-reply payloads. Meta's documented ceiling is 1000; the
#: value carries SPEC §6.2's ``node_id:button_id``, which is far shorter.
MAX_PAYLOAD_CHARS = 1000

#: Longest inbound text carried out of a parse. ``apps.messaging.ingest`` bounds
#: it again downstream; this one stops a hostile payload making us hold an
#: arbitrarily long string at all (SECURITY-BASELINE §§2, 7).
MAX_INBOUND_TEXT_CHARS = 4096

#: Longest attacker-supplied display string kept in ``payload.extra``.
MAX_EXTRA_CHARS = 200

#: The width of the columns a platform id has to fit. Longer ones are hashed
#: rather than cut, the rule this codebase applies to every identifier.
MAX_PLATFORM_ID_CHARS = 200

#: Attachment URLs carried out of a parse. Recorded, never fetched
#: (SECURITY-BASELINE §6).
MAX_URL_CHARS = 2000
MAX_ATTACHMENTS = 10

#: The queued action that answers a claimed comment. Registered at the foot of
#: this module, beside the adapter.
COMMENT_REPLY_ACTION = "instagram_comment_reply"

#: Where a claimed comment tells ``apps.messaging.ingest`` that a private reply
#: is permitted, so the messaging window opens for it, and where a message hands
#: it the platform's own id so a later deletion can find the row to redact.
#:
#: Literals rather than imports, because ``apps.channels`` sits below
#: ``apps.messaging`` and the keys are documented in the module that *reads*
#: them. That is the same shape ``apps.flows.triggers.pipeline`` uses for
#: ``ROUTING_PROCESSOR`` — and, like that one, the duplication is pinned by a
#: test (``test_instagram_policy.py::TestSharedExtraKeys``) so it cannot drift
#: silently. It would drift very quietly indeed: rename one side and every
#: comment-to-DM stops opening a window, so every private reply is Blocked by
#: the compliance engine and nothing raises anywhere.
PRIVATE_REPLY_CLAIMED_KEY = "private_reply_claimed"
PROVIDER_MESSAGE_ID_KEY = "provider_message_id"

#: Meta error codes meaning "this credential is finished" (SPEC §6.3). Anything
#: else is a message-level failure the send pipeline already classifies by HTTP
#: status; these are the ones a retry can never fix.
AUTH_ERROR_CODES = frozenset({"102", "190", "463", "467"})

#: "This person isn't available right now" — the account was deleted, or the
#: contact blocked us. Operationally identical to an opt-out: never send here again.
UNREACHABLE_ERROR_CODES = frozenset({"551"})

#: Block kind -> the ``attachment.type`` Instagram calls it. No ``file``:
#: Instagram messaging has no generic document attachment, which is why the
#: capability table leaves ``file`` False and the downgrade renderer turns one
#: into text before it ever reaches here.
_ATTACHMENT_TYPES: dict[str, str] = {"image": "image", "audio": "audio", "video": "video"}


# ---------------------------------------------------------------------------
# The Graph client
# ---------------------------------------------------------------------------

_POOL: httpx.Client | None = None
_POOL_LOCK = threading.Lock()


def _client() -> httpx.Client | None:
    """The pooled HTTP client every Graph call goes through.

    Kept for the life of the process, because ``request_json``'s default of one
    client per call means a fresh TCP connection and TLS handshake per message —
    a hundred milliseconds or more, inside SPEC §7.1's 1.5 s inline budget, paid
    again for every message of a downgraded gallery.

    Built lazily so a forked worker gets its own pool rather than inheriting
    sockets opened before the fork. **This is also the test seam**: a test
    monkeypatches this function to return an ``httpx.Client`` on a
    ``MockTransport`` and the whole module — the real error mapping, the real
    429 handling, the real payload building — runs without a socket.
    """
    global _POOL
    if _POOL is None:
        with _POOL_LOCK:
            if _POOL is None:
                _POOL = httpx.Client(limits=httpx.Limits(max_keepalive_connections=8, max_connections=32))
    return _POOL


def call(
    token: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    method: str = "POST",
    params: dict[str, Any] | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """One Graph call. Returns the decoded body.

    Raises :class:`~apps.channels.providers.exceptions.APIError` — or
    :class:`~apps.channels.providers.exceptions.RateLimitError` on a 429 — via
    ``request_json``, which also lifts Meta's ``error.code`` onto the exception
    so a caller can tell a dead token from a refused message.

    The token rides in an ``Authorization`` header rather than the
    ``access_token`` parameter Meta also accepts, so it never lands in a proxy
    access log (SECURITY-BASELINE §5).
    """
    if not token:
        raise APIError("This Instagram connection has no access token stored.")
    return request_json(
        method,
        f"{API_ROOT}/{API_VERSION}/{path}",
        json=payload,
        params=params,
        headers={"Authorization": f"Bearer {token}"},
        client=_client(),
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# Outbound: OutboundMessage -> Send API bodies
# ---------------------------------------------------------------------------


def wire_messages(recipient: dict[str, Any], message: OutboundMessage) -> list[dict[str, Any]]:
    """Request bodies for one already-downgraded message.

    Pure: no HTTP, no database, no clock. That is what lets the send-payload
    snapshots be a table, and what lets a reader check a body against Meta's
    reference without reading the send loop.

    ``recipient`` is ``{"id": <IGSID>}`` for an ordinary send and
    ``{"comment_id": <id>}`` for a private reply — the only difference between
    the two on the wire, which is why it is a parameter rather than two functions.

    Three assembly rules, in this order, because Instagram cannot express
    "text with buttons" directly:

    1. **Blocks become messages.** Text splits at the byte cap; media becomes an
       attachment with its caption following as its own bubble, because Meta's
       attachment payload has no caption field; cards and galleries accumulate
       into generic-template elements, ten to a message.
    2. **Quick replies ride on the last text message**, which is the only place
       Meta accepts them. With no text anywhere they become numbered options,
       the shared fallback wording from :mod:`apps.channels.downgrade`.
    3. **Buttons need a template.** They are appended to the last template's
       final element where there is one, and otherwise turn the last text
       message into a single-element template — the trailing eighty characters
       become its title and the rest stays as the preceding bubble, so nothing is
       duplicated and no copy is invented. With neither text nor template to
       hang them on they are left out and logged, the same visible failure
       Telegram's adapter chooses for a button it cannot represent.
    """
    parts: list[dict[str, Any]] = []
    elements: list[dict[str, Any]] = []

    for block in message.blocks:
        if isinstance(block, CardBlock):
            _collect_card(block.card, elements, parts, message.node_id)
            continue
        if isinstance(block, GalleryBlock):
            for card in block.cards:
                _collect_card(card, elements, parts, message.node_id)
            continue
        _flush_elements(elements, parts)
        if isinstance(block, TextBlock):
            parts.extend(_text_messages(block.text))
        elif isinstance(block, MediaBlock):
            parts.extend(_media_messages(block))
    _flush_elements(elements, parts)

    _attach_quick_replies(parts, message.quick_replies, message.node_id)
    _attach_buttons(parts, message.buttons, message.node_id)

    return [_body(recipient, part, message.tag) for part in parts]


def _body(recipient: dict[str, Any], message: dict[str, Any], tag: str | None) -> dict[str, Any]:
    """One Send API request body.

    ``messaging_type`` is ``MESSAGE_TAG`` only when the compliance engine put a
    tag on the message. It is never invented here: SPEC §22 hard-codes the
    human-agent allowance to inbox sends, and ``compliance.Allowed.apply``
    *replaces* the caller's tag precisely so an automation node cannot buy
    itself the seven-day escape by setting one.
    """
    body: dict[str, Any] = {"recipient": recipient, "message": message}
    if tag:
        body["messaging_type"] = "MESSAGE_TAG"
        body["tag"] = tag
    return body


def _flush_elements(elements: list[dict[str, Any]], parts: list[dict[str, Any]]) -> None:
    """Turn accumulated card elements into generic-template messages."""
    while elements:
        batch, elements[:] = elements[:MAX_TEMPLATE_ELEMENTS], elements[MAX_TEMPLATE_ELEMENTS:]
        parts.append({"attachment": {"type": "template", "payload": {"template_type": "generic", "elements": batch}}})


def _collect_card(card: Card, elements: list[dict[str, Any]], parts: list[dict[str, Any]], node_id: str = "") -> None:
    """Add one card as a template element, or as text when it cannot be one."""
    element = _card_element(card, node_id)
    if element is not None:
        elements.append(element)
        return
    # Meta requires a title plus at least one other property. A card carrying
    # only a title is not a card the platform will accept, and text is a better
    # answer than a rejected send.
    _flush_elements(elements, parts)
    parts.extend(_text_messages(_card_text(card)))


def _card_element(card: Card, node_id: str = "") -> dict[str, Any] | None:
    """One generic-template element, or None when Meta would reject it.

    ``node_id`` is threaded in so a card's postback payloads carry SPEC §6.2's
    ``node_id:button_id`` exactly as message-level buttons do. It is decoration
    — the engine matches a press on the button id against the waiting node's
    handles — but two halves of one message encoding differently is the kind of
    inconsistency that costs somebody an afternoon later.
    """
    title = (card.title or card.subtitle or card.url or "").strip()[:MAX_TITLE_CHARS]
    if not title:
        return None
    element: dict[str, Any] = {"title": title}
    if card.subtitle and card.subtitle.strip()[:MAX_TITLE_CHARS] != title:
        element["subtitle"] = card.subtitle.strip()[:MAX_TITLE_CHARS]
    if card.image_url:
        element["image_url"] = card.image_url
    if card.url:
        element["default_action"] = {"type": "web_url", "url": card.url}
    buttons = _buttons(card.buttons, node_id)
    if buttons:
        element["buttons"] = buttons
    # "At least one property must be set in addition to title."
    return element if len(element) > 1 else None


def _card_text(card: Card) -> str:
    """A card as plain text. Only reached for a card Meta would refuse."""
    return "\n".join(part for part in (card.title, card.subtitle, card.url) if part)


def _text_messages(text: str) -> list[dict[str, Any]]:
    """``{"text": ...}`` messages for ``text``, split to fit Meta's byte cap.

    Meta rejects an empty ``text`` outright, so a blank block produces no message
    at all rather than one that fails at the platform.
    """
    return [{"text": part} for part in _text_parts(text)]


def _text_parts(text: str) -> list[str]:
    if not text.strip():
        return []
    parts: list[str] = []
    for chunk in split_text(text, MAX_TEXT_CHARS):
        parts.extend(_within_bytes(chunk))
    return [part for part in parts if part.strip()]


def _within_bytes(chunk: str, depth: int = 0) -> list[str]:
    """Re-split ``chunk`` until every piece fits :data:`MAX_TEXT_BYTES`.

    ``split_text`` counts characters, and Meta counts bytes. A thousand
    characters of CJK or emoji is three or four thousand bytes, so a cap applied
    in characters alone passes a message the platform then rejects — silently
    turning a flow's reply into a failed send for one class of author and not
    another.

    The character budget is re-derived from the piece's own encoded length, so it
    strictly shrinks on each pass and the recursion terminates; ``depth`` is a
    backstop against a pathological input, not an expected path.
    """
    encoded = len(chunk.encode("utf-8"))
    if encoded <= MAX_TEXT_BYTES:
        return [chunk]
    if depth >= MAX_SPLIT_DEPTH:
        # Convergence has not been observed on real text, which is exactly why
        # this branch must not hand the platform a piece it will reject with a
        # 400 the send pipeline then reports as a bare ``provider_rejected``.
        # Cut on the encoded bytes instead — ``errors="ignore"`` drops the
        # partial character a byte slice can leave behind — and say so.
        logger.warning("Instagram: text did not split within %s passes and was cut to fit.", MAX_SPLIT_DEPTH)
        return [chunk.encode("utf-8")[:MAX_TEXT_BYTES].decode("utf-8", errors="ignore")]
    budget = max(1, len(chunk) * MAX_TEXT_BYTES // encoded)
    pieces: list[str] = []
    for piece in split_text(chunk, budget):
        pieces.extend(_within_bytes(piece, depth + 1))
    return pieces


def _media_messages(block: MediaBlock) -> list[dict[str, Any]]:
    """An attachment, and its caption as the bubble after it.

    Meta's attachment payload carries a URL and nothing else — there is no
    caption field — so a caption becomes its own message. It follows the
    attachment rather than preceding it because that is the order the blocks
    were authored in and because it gives message-level buttons a text bubble to
    attach to (see :func:`wire_messages`).
    """
    kind = _ATTACHMENT_TYPES.get(block.kind)
    if kind is None or not block.url:
        # An unsupported kind should already have become text upstream, and a
        # media block with no address cannot be sent at all. Either way the
        # caption and the link are the only things left worth delivering.
        return _text_messages("\n".join(part for part in (block.caption, block.url) if part))
    messages: list[dict[str, Any]] = [{"attachment": {"type": kind, "payload": {"url": block.url}}}]
    messages.extend(_text_messages(block.caption))
    return messages


def _attach_quick_replies(parts: list[dict[str, Any]], quick_replies: tuple[QuickReply, ...], node_id: str) -> None:
    """Put quick replies on the last text message.

    Meta accepts ``quick_replies`` only beside ``message.text``, so a message
    with no text bubble anywhere — a caption-less image, say — has nowhere to put
    them, and they are dropped with a warning. That is the same visible failure
    :func:`_attach_buttons` chooses, and it replaced something worse: rendering
    "Reply 1 for Yes" here looked like the shared numbered-option fallback and
    was not one. That fallback only works because
    ``apps.flows.engine.nodes.send_message`` rebuilds the number-to-id map by
    re-running ``downgrade`` — and ``downgrade`` produces no numbers for
    Instagram, which declares ``quick_replies=True``. The contact was being shown
    instructions that resolved to nothing.
    """
    if not quick_replies:
        return
    chips = [
        {
            "content_type": "text",
            "title": _label(item)[:MAX_LABEL_CHARS],
            "payload": payload,
        }
        for item, payload in ((item, _payload(node_id, item.id)) for item in quick_replies)
        if _label(item) and payload
    ]
    if not chips:
        return
    for part in reversed(parts):
        if "text" in part:
            part["quick_replies"] = chips
            return
    logger.warning(
        "Instagram: %s quick repl(ies) had no text message to ride on and were left out. "
        "Give the message a text block or a media caption to carry them.",
        len(chips),
    )


def _attach_buttons(parts: list[dict[str, Any]], buttons: tuple[Button, ...], node_id: str) -> None:
    """Give the message's buttons a generic template to live on.

    Instagram has no "text with buttons" message; buttons exist only on a
    template element. So there are exactly two places to put them, tried in
    order, and one honest failure.
    """
    rendered = _buttons(buttons, node_id)
    if not rendered:
        return

    # 1. A card is already the last thing being sent. Append, up to Meta's three
    #    per element; dropping the overflow beats a rejected send.
    elements = _template_elements(parts[-1]) if parts else None
    if elements:
        existing = list(elements[-1].get("buttons") or [])[:MAX_TEMPLATE_BUTTONS]
        elements[-1]["buttons"] = [*existing, *rendered[: MAX_TEMPLATE_BUTTONS - len(existing)]]
        return

    # 2. Otherwise the last plain text message becomes one. Its trailing eighty
    #    characters are the card's title and the rest stays as the bubble before
    #    it, so nothing is duplicated and no copy is invented. A text message
    #    already carrying quick replies is skipped: it would lose them.
    for index in reversed(range(len(parts))):
        part = parts[index]
        if "text" not in part or "quick_replies" in part:
            continue
        head, title = _split_title(part["text"])
        element: dict[str, Any] = {"title": title, "buttons": rendered}
        parts[index : index + 1] = [
            *_text_messages(head),
            {"attachment": {"type": "template", "payload": {"template_type": "generic", "elements": [element]}}},
        ]
        return

    # 3. Neither. A media-only message has no title to give a card and Meta
    #    requires one, so the buttons are left out — visibly and loudly, and
    #    without a numbered fallback, because the engine's number-to-id map is
    #    rebuilt by re-running ``downgrade``, which keeps buttons natively for a
    #    platform declaring ``buttons=True`` and so records no numbers to match.
    #    Inventing numbers here would show the contact instructions that resolve
    #    to nothing. See ``docs/channels/instagram.md``.
    logger.warning(
        "Instagram: %s button(s) had no text or card to attach to and were left out. "
        "Give the message a text block or a media caption to carry them.",
        len(rendered),
    )


def _template_elements(part: dict[str, Any]) -> list[dict[str, Any]] | None:
    """The element list of a generic-template message, or None."""
    attachment = part.get("attachment")
    if not isinstance(attachment, dict) or attachment.get("type") != "template":
        return None
    payload = attachment.get("payload")
    if not isinstance(payload, dict):
        return None
    elements = payload.get("elements")
    return elements if isinstance(elements, list) and elements else None


def _split_title(text: str) -> tuple[str, str]:
    """``(everything before, the trailing <=80 characters)``, on a word boundary.

    Nothing is duplicated and nothing is invented: the tail of the author's own
    message becomes the card title the buttons sit under, and the head stays as
    the bubble before it.
    """
    stripped = text.strip()
    if len(stripped) <= MAX_TITLE_CHARS:
        return "", stripped
    tail = stripped[-MAX_TITLE_CHARS:]
    cut = tail.find(" ")
    if 0 <= cut < MAX_TITLE_CHARS - 1:
        tail = tail[cut + 1 :]
    return stripped[: len(stripped) - len(tail)].rstrip(), tail


def _buttons(buttons: tuple[Button, ...], node_id: str) -> list[dict[str, Any]]:
    """Meta's ``web_url`` and ``postback`` buttons. Only those two are supported."""
    rendered: list[dict[str, Any]] = []
    for button in buttons[:MAX_TEMPLATE_BUTTONS]:
        label = _label(button)[:MAX_LABEL_CHARS]
        if not label:
            continue
        if button.is_url:
            rendered.append({"type": "web_url", "url": button.url, "title": label})
            continue
        payload = _payload(node_id, button.id)
        if payload:
            rendered.append({"type": "postback", "title": label, "payload": payload})
    return rendered


def _label(item: Button | QuickReply) -> str:
    """Instagram rejects an empty label; fall back to the id."""
    return (item.label or item.id).strip()


def _payload(node_id: str, target_id: str) -> str:
    """SPEC §6.2's ``node_id:button_id``, shared with Telegram's ``callback_data``.

    **The separator is always present**, even with no node — a message from the
    inbox or the public API has none, and it encodes as ``:<id>``. That costs one
    character and buys an unambiguous decoding, because a button id *may* contain
    a colon: the graph schema forbids it, but nothing constrains an
    ``OutboundMessage`` built by hand, and ``"a:b"`` on the wire would come back
    as ``"b"`` and match no handle.
    """
    if not target_id:
        return ""
    return f"{node_id}:{target_id}"[:MAX_PAYLOAD_CHARS]


def _button_id(payload: str) -> str:
    """The button half of a postback payload. Split on the **first** colon."""
    return payload.split(":", 1)[-1]


# ---------------------------------------------------------------------------
# Inbound: one delivery -> NormalizedEvents
# ---------------------------------------------------------------------------


def _text(value: Any, limit: int = MAX_INBOUND_TEXT_CHARS) -> str:
    """A bounded string, or "". Every inbound field goes through this."""
    return meta_common.bounded_text(value, limit)


def _platform_id(value: Any) -> str:
    """A platform id, bounded by **hashing** rather than by truncation.

    The bounding itself is ``apps.messaging.identities.bounded_key``, reached
    through the ``apps.flows.messaging`` facade rather than re-derived here. That
    is not tidiness: :func:`_pending_private_reply` compares the result against a
    ``commenter_ref`` that ``triggers.guards`` bounded with that same function,
    and the facade's own docstring says why a local copy is wrong — "re-deriving
    it locally would be a second implementation that silently stops agreeing".
    Two implementations that must agree are one implementation with a delay.

    What is left here is the part that is genuinely this parser's: Meta sends
    ids as JSON numbers as well as strings, and ``bool`` is an ``int`` in Python.
    """
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return ""
    return messaging_facade.bounded_identifier(str(value), limit=MAX_PLATFORM_ID_CHARS)


def _moment(raw: Any, *, milliseconds: bool) -> datetime:
    """A platform timestamp, or now.

    A wrong clock on an event is cosmetic; refusing the event because its
    timestamp was a string is a lost message. ``fromtimestamp`` raises outside
    the platform's range, which is exactly what a hostile payload sends.
    """
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        try:
            return datetime.fromtimestamp(raw / 1000 if milliseconds else raw, UTC)
        except (OverflowError, OSError, ValueError):
            pass
    return timezone.now()


def _attachment_urls(message: dict[str, Any]) -> tuple[str, ...]:
    """Attachment URLs, recorded and never fetched (SECURITY-BASELINE §6)."""
    raw = message.get("attachments")
    if not isinstance(raw, list):
        return ()
    urls: list[str] = []
    for item in raw[:MAX_ATTACHMENTS]:
        if not isinstance(item, dict):
            continue
        payload = item.get("payload")
        url = _text(payload.get("url"), MAX_URL_CHARS) if isinstance(payload, dict) else ""
        if url:
            urls.append(url)
    return tuple(urls)


def _attachment_kinds(message: dict[str, Any]) -> tuple[str, ...]:
    raw = message.get("attachments")
    if not isinstance(raw, list):
        return ()
    return tuple(_text(item.get("type"), MAX_EXTRA_CHARS) for item in raw[:MAX_ATTACHMENTS] if isinstance(item, dict))


def _story_extra(message: dict[str, Any]) -> dict[str, Any]:
    """The story a mention or a reply refers to. Attacker-controlled: escape on render."""
    extra: dict[str, Any] = {}
    reply_to = message.get("reply_to")
    story = reply_to.get("story") if isinstance(reply_to, dict) else None
    if isinstance(story, dict):
        for key, limit in (("id", MAX_PLATFORM_ID_CHARS), ("url", MAX_URL_CHARS)):
            value = _text(story.get(key), limit)
            if value:
                extra[f"story_{key}"] = value
    return extra


# ---------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------


class InstagramAdapter(Adapter):
    """SPEC §6.3, implemented against SPEC §6.1's interface."""

    platform = Platform.INSTAGRAM.value
    capabilities = _CAPABILITIES
    webhook_content = "json"

    # -- inbound ------------------------------------------------------------

    def resolve_connection(self, request: "HttpRequest", raw_body: bytes) -> ChannelConnection | None:
        """Which account this delivery is for, from the first entry's id.

        SPEC §7.1 gives Meta one ``/webhooks/instagram/`` per deployment with
        "connection resolved from payload ids", so this necessarily reads an
        unverified body — the signature can only be checked once the connection,
        and therefore the app secret, is known. The endpoint bounds what that
        costs: the size cap and the JSON nesting cap both run first, and the
        parse is the endpoint's own cached one.

        Only the **first** entry is used to pick the connection to verify
        against. A batch spanning several accounts is normal, and each event
        names its own connection (:meth:`parse_events`); what the framework then
        guarantees is that a secondary connection must clear the same gates and
        belong to the same workspace as the verified one
        (``views_webhooks._event_connection``).
        """
        payload = security.parse_json_body(raw_body) or {}
        if payload.get("object") != WEBHOOK_OBJECT:
            return None
        for entry in meta_common.entries(payload):
            connection = _connection_for_entry(entry)
            if connection is not None:
                return connection
        return None

    def verify_webhook(self, request: "HttpRequest", connection: ChannelConnection) -> bool:
        """``X-Hub-Signature-256`` over the raw body, with the app secret."""
        return meta_common.verify_hub_signature(request, connection)

    def parse_events(self, request: "HttpRequest", connection: ChannelConnection) -> list[NormalizedEvent]:
        """Turn one verified delivery into normalized events.

        Defensive by contract (SECURITY-BASELINE §2): every value here was typed
        by a stranger. Nothing raises, nothing assumes a key exists, everything
        is length-bounded, and an item we do not understand produces no event
        rather than a half-populated one.
        """
        payload = security.json_payload(request) or {}
        if payload.get("object") != WEBHOOK_OBJECT:
            logger.info("Instagram delivery on connection %s declared another object; ignored.", connection.pk)
            return []

        events: list[NormalizedEvent] = []
        for entry in meta_common.entries(payload):
            # No fallback to the delivery-level connection for an entry that
            # names no account. Meta always sends ``entry[].id``, so an entry
            # without one is not a delivery to attribute — and attributing it to
            # whichever connection happened to verify the signature would file a
            # stranger's messages into a thread on an account the payload never
            # mentioned.
            owner = _connection_for_entry(entry)
            if owner is None:
                logger.info("Instagram delivery named an account with no connection on this deployment.")
                continue
            events.extend(_entry_events(owner, entry))
        return events

    # -- outbound -----------------------------------------------------------

    def send(self, connection: ChannelConnection, identity: Any, outbound: OutboundMessage) -> SendResult:
        """Deliver one message, downgrading it first (SPEC §6.1).

        **A multi-part send is not atomic**, and cannot be here: a gallery
        becomes several calls, and if the third fails the first two have already
        arrived. SPEC §9.4 keys idempotency on the message row, of which there is
        one, so a retry sends all three again. Duplicate rather than drop is the
        right direction for a message a flow author intended to send; the fix is
        per-part progress on the row, which is a schema change and a decision for
        every adapter rather than this one. Telegram's adapter says the same.

        The first message to a contact who reached us through a comment goes out
        as a **private reply**, addressed by comment id rather than by user id —
        see :func:`_pending_private_reply`.
        """
        recipient_id = _platform_id(getattr(identity, "platform_user_id", "") or "")
        if not recipient_id:
            return SendResult(status=SendStatus.FAILED, error="no_recipient")

        token = access_token(connection)
        claimed = _pending_private_reply(connection, identity)
        recipient: dict[str, Any] = {"comment_id": claimed.comment_id} if claimed is not None else {"id": recipient_id}

        rendered = downgrade(outbound, self.capabilities)
        bodies: list[dict[str, Any]] = []
        for message in rendered.messages:
            bodies.extend(wire_messages(recipient, message))
        if not bodies:
            # Nothing sendable survived. Reported rather than silently counted as
            # sent, so contract 1's message row says what happened.
            return SendResult(status=SendStatus.FAILED, error="empty_message")

        if claimed is not None and len(bodies) > 1:
            logger.info("Instagram: only the first message of a %s-part reply is a private reply.", len(bodies))

        provider_message_id = ""
        for index, body in enumerate(bodies):
            if index and claimed is not None:
                # Meta allows exactly one private reply per comment, so only the
                # first message is addressed that way. The rest go to the person
                # — which works precisely because the private reply opened the
                # thread a moment ago.
                body = {**body, "recipient": {"id": recipient_id}}
            try:
                result = call(token, "me/messages", body)
            except APIError as exc:
                self._handle_send_error(connection, recipient_id, exc)
                raise
            provider_message_id = _text(result.get("message_id"), MAX_PLATFORM_ID_CHARS) or provider_message_id
            if index == 0 and claimed is not None:
                _spend_private_reply(claimed, identity, result)
        return SendResult(status=SendStatus.SENT, provider_message_id=provider_message_id)

    def _handle_send_error(self, connection: ChannelConnection, recipient_id: str, exc: APIError) -> None:
        """Classify a Graph rejection, then let the error carry on.

        Two of Meta's codes mean something structural rather than "this message
        was refused", and both have to be acted on or the channel degrades
        silently: a dead credential stops everything until somebody reconnects,
        and an unreachable contact must never be sent to again.

        The adapter does **not** write ``identity.opted_out_at`` itself — ROADMAP
        contract 3 reserves that column for the ingest pipeline — so it raises
        the event the pipeline already knows how to apply, which also lets every
        other hook on the seam see it.
        """
        if exc.code in AUTH_ERROR_CODES:
            mark_needs_reauth(connection)
            return
        if exc.code not in UNREACHABLE_ERROR_CODES:
            return
        logger.info("Instagram: connection %s can no longer reach a contact; recording an opt-out.", connection.pk)
        now = timezone.now()
        event = NormalizedEvent(
            type=EventType.OPT_OUT,
            connection=connection,
            platform_user_id=recipient_id,
            # Timestamped rather than content-only: someone who blocks, unblocks
            # and blocks again is two events, not one duplicate.
            provider_event_id=f"ig:blocked:{recipient_id}:{int(now.timestamp())}",
            timestamp=now,
            payload=EventPayload(extra={"reason": "unreachable"}),
        )
        try:
            channels_ingest.process_events(connection, (event,))
        except Exception:
            # The send failure is what the caller is waiting to hear about.
            logger.exception("Instagram: could not record an opt-out on connection %s.", connection.pk)

    def send_typing(self, connection: ChannelConnection, identity: Any) -> None:
        """``typing_on``. Cosmetic, so a failure is logged and swallowed."""
        self._sender_action(connection, identity, "typing_on")

    def mark_seen(self, connection: ChannelConnection, identity: Any) -> None:
        """``mark_seen``. Cosmetic, so a failure is logged and swallowed."""
        self._sender_action(connection, identity, "mark_seen")

    def _sender_action(self, connection: ChannelConnection, identity: Any, action: str) -> None:
        recipient_id = _platform_id(getattr(identity, "platform_user_id", "") or "")
        if not recipient_id:
            return
        try:
            call(
                access_token(connection),
                "me/messages",
                {"recipient": {"id": recipient_id}, "sender_action": action},
            )
        except APIError:
            logger.debug("Instagram: %s failed on connection %s.", action, connection.pk)


# ---------------------------------------------------------------------------
# Connection resolution
# ---------------------------------------------------------------------------


def _entry_id(entry: dict[str, Any]) -> str:
    return _platform_id(entry.get("id"))


def _connection_for_entry(entry: dict[str, Any]) -> ChannelConnection | None:
    """The connection whose account this entry names, or None.

    Cross-tenant by necessity: an inbound webhook has no session and therefore
    no workspace, and the account id in the payload is the only key. What bounds
    it is the endpoint — ``views_webhooks._usable`` checks the platform and the
    status of whatever comes back, and ``_event_connection`` refuses a secondary
    connection in a different workspace from the verified one.
    """
    account_id = _entry_id(entry)
    if not account_id:
        return None
    return (
        ChannelConnection.objects.unscoped()
        .filter(platform=Platform.INSTAGRAM.value, external_id=account_id)
        .select_related("workspace")
        .first()
    )


def _entry_events(connection: ChannelConnection, entry: dict[str, Any]) -> list[NormalizedEvent]:
    """Every event one ``entry`` carries: DMs first, then comments and mentions."""
    account_id = _entry_id(entry)
    events: list[NormalizedEvent] = []
    for item in meta_common.messaging(entry):
        events.extend(_messaging_events(connection, item, entry))
    for change in meta_common.changes(entry):
        events.extend(_change_events(connection, change, entry, account_id))
    return events


def _event_id(item: dict[str, Any], mid: str, prefix: str, *, at: Any = None) -> str:
    """A deduplication key (SPEC §7.1 step 2), from the platform's id or content.

    Meta gives most things a stable id; the ones it does not — a referral, some
    follow shapes — are hashed from their own content.

    ``at`` is the arrival time, and it is not optional decoration. A ``messaging``
    item carries its own ``timestamp`` and hashes distinctly on its own, but a
    ``changes`` item does not: a ``follows`` value is ``{"from": {...}}`` and
    nothing else, so a follow, an unfollow and a re-follow a week later all hash
    to the same digest and the second and third are discarded by
    ``webhook_event_log``'s unique ``(connection, provider_event_id)`` as
    redeliveries. ``synthetic_event_id``'s own docstring names this trap and says
    the way out is to include the platform's timestamp — the entry's ``time`` is
    what that is here, so callers reading a ``changes`` item pass it.
    """
    if mid:
        return f"{prefix}{mid}"
    payload = item if at is None else {"item": item, "at": at}
    return channels_ingest.synthetic_event_id(payload, prefix=prefix)


def _messaging_events(
    connection: ChannelConnection,
    item: dict[str, Any],
    entry: dict[str, Any],
) -> list[NormalizedEvent]:
    """One ``messaging`` item: a DM, a postback, a referral or a deletion."""
    if meta_common.is_echo(item):
        # A copy of something we sent, or a DM the account sent from the phone.
        # Ingesting one files our own outbound text as the contact's inbound
        # reply, which then matches a keyword trigger and answers itself.
        return []

    sender = item.get("sender")
    sender_id = _platform_id(sender.get("id")) if isinstance(sender, dict) else ""
    if not sender_id:
        return []
    when = _moment(item.get("timestamp"), milliseconds=True)

    message = item.get("message")
    if isinstance(message, dict):
        return _message_events(connection, item, message, sender_id, when)

    postback = item.get("postback")
    if isinstance(postback, dict):
        return _postback_events(connection, item, postback, sender_id, when)

    referral = item.get("referral")
    if isinstance(referral, dict):
        return _referral_events(connection, item, referral, sender_id, when)

    if _looks_like_follow(item):
        return _follow_event(connection, item, sender_id, when)
    return []


def _message_events(
    connection: ChannelConnection,
    item: dict[str, Any],
    message: dict[str, Any],
    sender_id: str,
    when: datetime,
) -> list[NormalizedEvent]:
    mid = _platform_id(message.get("mid"))

    if message.get("is_deleted"):
        # SPEC §6.3 / §19. The body is redacted and the row kept, which
        # ``apps.messaging.ingest`` does from the id in ``extra``.
        if not mid:
            return []
        return [
            NormalizedEvent(
                type=EventType.MESSAGE_DELETED,
                connection=connection,
                platform_user_id=sender_id,
                provider_event_id=f"ig:del:{mid}",
                timestamp=when,
                payload=EventPayload(extra={PROVIDER_MESSAGE_ID_KEY: mid}),
                raw=item,
            )
        ]

    quick_reply = message.get("quick_reply")
    if isinstance(quick_reply, dict):
        payload = _text(quick_reply.get("payload"), MAX_PAYLOAD_CHARS)
        if payload:
            # A quick reply comes back as a message with a payload, and it means
            # exactly what a button press means — SPEC §7.2's ``button id``.
            return [
                NormalizedEvent(
                    type=EventType.POSTBACK,
                    connection=connection,
                    platform_user_id=sender_id,
                    provider_event_id=_event_id(item, mid, "ig:"),
                    timestamp=when,
                    payload=EventPayload(
                        button_id=_button_id(payload),
                        text=_text(message.get("text")),
                        extra={"payload": payload},
                    ),
                    raw=item,
                )
            ]

    text = _text(message.get("text"))
    attachments = _attachment_urls(message)
    kinds = _attachment_kinds(message)
    extra: dict[str, Any] = {}
    if kinds:
        extra["attachment_types"] = list(kinds)

    if "story_mention" in kinds:
        # SPEC §10's story-mention trigger. Delivered as a DM with a
        # ``story_mention`` attachment rather than as its own webhook field.
        return [
            NormalizedEvent(
                type=EventType.STORY_MENTION,
                connection=connection,
                platform_user_id=sender_id,
                provider_event_id=_event_id(item, mid, "ig:"),
                timestamp=when,
                payload=EventPayload(text=text, attachments=attachments, extra=extra),
                raw=item,
            )
        ]

    story = _story_extra(message)
    if story:
        # A reply to one of *our* stories. SPEC §10's story-reply trigger takes
        # optional keywords, so the text has to travel with it.
        return [
            NormalizedEvent(
                type=EventType.STORY_REPLY,
                connection=connection,
                platform_user_id=sender_id,
                provider_event_id=_event_id(item, mid, "ig:"),
                timestamp=when,
                payload=EventPayload(text=text, attachments=attachments, extra={**extra, **story}),
                raw=item,
            )
        ]

    if not text and not attachments:
        # A reaction, a read receipt shape we do not carry, an unshared media.
        # Nothing a contact said, so nothing to file.
        return []

    if mid:
        # Recorded so a later ``message_deletions`` delivery can find this row.
        # ``apps.messaging.ingest`` reads the key; the convention is documented
        # there, beside the delivery-receipt one it already owns.
        extra[PROVIDER_MESSAGE_ID_KEY] = mid

    return [
        NormalizedEvent(
            type=EventType.MESSAGE,
            connection=connection,
            platform_user_id=sender_id,
            provider_event_id=_event_id(item, mid, "ig:"),
            timestamp=when,
            payload=EventPayload(text=text, attachments=attachments, extra=extra),
            raw=item,
        )
    ]


def _postback_events(
    connection: ChannelConnection,
    item: dict[str, Any],
    postback: dict[str, Any],
    sender_id: str,
    when: datetime,
) -> list[NormalizedEvent]:
    payload = _text(postback.get("payload"), MAX_PAYLOAD_CHARS)
    mid = _platform_id(postback.get("mid"))
    if not payload:
        return []
    extra: dict[str, Any] = {"payload": payload}
    # An ig.me link that opened the thread carries its ref on the postback.
    referral = postback.get("referral")
    ref = _text(referral.get("ref"), MAX_EXTRA_CHARS) if isinstance(referral, dict) else ""
    return [
        NormalizedEvent(
            type=EventType.POSTBACK,
            connection=connection,
            platform_user_id=sender_id,
            provider_event_id=_event_id(item, mid, "ig:pb:"),
            timestamp=when,
            payload=EventPayload(button_id=_button_id(payload), ref=ref, extra=extra),
            raw=item,
        )
    ]


def _referral_events(
    connection: ChannelConnection,
    item: dict[str, Any],
    referral: dict[str, Any],
    sender_id: str,
    when: datetime,
) -> list[NormalizedEvent]:
    """An ig.me deep link. SPEC §10's Ref URL trigger reads ``payload.ref``.

    A referral with no ref is not dropped: ``apps.flows.triggers.matching``
    treats exactly that as "arrived with no payload", which is the normalised
    shape of a conversation being opened.
    """
    ref = _text(referral.get("ref"), MAX_EXTRA_CHARS)
    return [
        NormalizedEvent(
            type=EventType.REFERRAL,
            connection=connection,
            platform_user_id=sender_id,
            provider_event_id=_event_id(item, "", "ig:ref:"),
            timestamp=when,
            payload=EventPayload(ref=ref, extra={"source": _text(referral.get("source"), MAX_EXTRA_CHARS)}),
            raw=item,
        )
    ]


#: ``changes`` fields that would carry a new follower, if Meta ever delivers one
#: to this product. See :func:`_follow_event`.
FOLLOW_FIELDS = frozenset({"follows", "followers"})


def _looks_like_follow(item: dict[str, Any]) -> bool:
    return isinstance(item.get("follow"), dict) or item.get("event") == "follow"


def _follow_event(
    connection: ChannelConnection,
    item: dict[str, Any],
    sender_id: str,
    when: datetime,
    *,
    at: Any = None,
) -> list[NormalizedEvent]:
    """SPEC §10's follow trigger, which degrades gracefully by design.

    The Instagram API with Instagram Login publishes **no follow webhook field**.
    SPEC §10 anticipated that — "fires on new-follower webhook where available;
    degrade gracefully if the field is unavailable to the app" — so the parser
    exists, is tested against the shape Meta would plausibly use, and simply
    never fires on a real deployment. That is a deliberate choice over deleting
    the trigger type: an app granted the field later needs no code, and an author
    who configures the trigger sees it listed rather than missing.
    """
    return [
        NormalizedEvent(
            type=EventType.FOLLOW,
            connection=connection,
            platform_user_id=sender_id,
            provider_event_id=_event_id(item, "", "ig:follow:", at=at),
            timestamp=when,
            payload=EventPayload(),
            raw=item,
        )
    ]


def _change_events(
    connection: ChannelConnection,
    change: dict[str, Any],
    entry: dict[str, Any],
    account_id: str,
) -> list[NormalizedEvent]:
    field = _text(change.get("field"), MAX_EXTRA_CHARS)
    value = change.get("value")
    if not isinstance(value, dict):
        return []
    when = _moment(entry.get("time"), milliseconds=False)
    if field == "comments":
        return _comment_event(connection, change, value, account_id, when)
    if field == "mentions":
        return _mention_event(connection, change, value, account_id, when)
    if field in FOLLOW_FIELDS:
        commenter = value.get("from")
        sender_id = _platform_id(commenter.get("id")) if isinstance(commenter, dict) else ""
        # ``at`` because a ``follows`` change carries no time of its own; see
        # :func:`_event_id`.
        return _follow_event(connection, change, sender_id, when, at=entry.get("time")) if sender_id else []
    return []


def _comment_event(
    connection: ChannelConnection,
    change: dict[str, Any],
    value: dict[str, Any],
    account_id: str,
    when: datetime,
) -> list[NormalizedEvent]:
    """A comment on one of this account's posts (SPEC §10's comment trigger).

    The keys this fills in ``payload.extra`` are L4-A's published contract —
    :mod:`apps.flows.triggers.types` — and nothing else about a comment travels
    anywhere: the guard, the matcher and the once-per-post rule all read these.
    """
    comment_id = _platform_id(value.get("id"))
    if not comment_id:
        return []

    author = value.get("from")
    commenter_id = _platform_id(author.get("id")) if isinstance(author, dict) else ""
    if not commenter_id:
        # Unattributable, and therefore unusable in both directions. SPEC §10's
        # once-per-commenter-per-post guard keys on the commenter, so an empty
        # one collides with every other empty one and the *first* such comment
        # locks out everybody else on that post; and the private reply cannot
        # open a DM thread without an address to open it to. The self-reply
        # filter below also depends on it, so an anonymous comment would slip
        # past that too. Dropping is the only answer that is right on all three.
        logger.info("Instagram comment on connection %s carried no author; ignored.", connection.pk)
        return []
    if account_id and commenter_id == account_id:
        # Our own public reply comes straight back as a comment webhook. Acting
        # on it would let a comment trigger answer its own reply, forever.
        return []

    media = value.get("media")
    post_id = _platform_id(media.get("id")) if isinstance(media, dict) else ""
    extra: dict[str, Any] = {COMMENT_POST_ID_KEY: post_id}
    parent_id = _platform_id(value.get("parent_id"))
    if parent_id:
        # Absent means top level, which is what ``top_level_only`` switches on.
        extra[COMMENT_PARENT_ID_KEY] = parent_id
    username = _text(author.get("username"), MAX_EXTRA_CHARS) if isinstance(author, dict) else ""
    if username:
        extra["username"] = username

    return [
        NormalizedEvent(
            type=EventType.COMMENT,
            connection=connection,
            platform_user_id=commenter_id,
            provider_event_id=f"ig:c:{comment_id}",
            timestamp=when,
            payload=EventPayload(text=_text(value.get("text")), comment_id=comment_id, extra=extra),
            raw=change,
        )
    ]


def _mention_event(
    connection: ChannelConnection,
    change: dict[str, Any],
    value: dict[str, Any],
    account_id: str,
    when: datetime,
) -> list[NormalizedEvent]:
    """An ``@mention`` of this account, in a comment or in a caption.

    Both shapes are dropped today, for the same reason and not for want of
    trying: **Meta's ``mentions`` value names no author.** A caption mention
    carries a media id and nothing else; a comment mention adds a comment id and
    the text. Neither says who wrote it.

    Without an author there is nothing to key SPEC §10's
    once-per-commenter-per-post guard on and no address to open a DM thread to.
    Emitting one anyway meant every mention on a post shared the empty
    ``commenter_ref``, so the first one claimed the guard and locked out
    everybody else — while itself failing to open a thread. Answering mentions
    needs a ``GET /{ig-comment-id}?fields=from``, which is a Graph round trip
    inside the webhook ack path, so it is out of scope here rather than done
    badly. See ``docs/channels/instagram.md``.

    Parsed to the point of proving what is and is not there, so the day Meta adds
    an author this is one field rather than a new code path.
    """
    comment_id = _platform_id(value.get("comment_id"))
    author = value.get("from")
    commenter_id = _platform_id(author.get("id")) if isinstance(author, dict) else ""
    if not comment_id or not commenter_id:
        logger.debug(
            "Instagram mention on connection %s named no comment or no author; ignored.",
            connection.pk,
        )
        return []
    if account_id and commenter_id == account_id:
        return []
    extra: dict[str, Any] = {
        COMMENT_POST_ID_KEY: _platform_id(value.get("media_id")),
        "mention": True,
    }
    return [
        NormalizedEvent(
            type=EventType.COMMENT,
            connection=connection,
            platform_user_id=commenter_id,
            provider_event_id=f"ig:m:{comment_id}",
            timestamp=when,
            payload=EventPayload(text=_text(value.get("text")), comment_id=comment_id, extra=extra),
            raw=change,
        )
    ]


# ---------------------------------------------------------------------------
# Comment to DM (SPEC §10)
# ---------------------------------------------------------------------------


def _pending_private_reply(connection: ChannelConnection, identity: Any) -> Any:
    """The claimed comment this contact's first message is the reply to, or None.

    Meta will not accept an ordinary DM to somebody who has never messaged the
    account. What it accepts is a **private reply**, addressed by comment id,
    once per comment and within seven days — so the flow's first message has to
    go out that way, and every message after it by user id.

    Deciding it here rather than threading a flag through
    ``OutboundMessage`` keeps the platform detail in the platform module: the
    flow engine sends a message to a contact and does not need to learn that
    Instagram has two kinds of recipient.

    **Only when there is no other way to reach them.** An unanswered claim is not
    on its own a licence to readdress a send: a claim whose flow never started —
    no publishable version, a refused first send — sits open for seven days, and
    without this check an agent's inbox reply days later would go out as that
    comment's one private reply, carrying Meta's auto-appended link to the post
    and spending an allowance the agent knew nothing about. So the claim is
    consumed only when the contact has never messaged the account
    (:func:`_never_messaged_us`), which is the one case where a private reply is
    both correct and the only form Meta will accept.

    Two indexed lookups on the send path, and no cheaper pre-filter in front of
    the first. The obvious one — gating on ``identity.last_inbound_at`` — is
    wrong for the reason :func:`_never_messaged_us` gives. ``HandledComment``
    carries a partial index over exactly the unanswered rows
    (:class:`apps.flows.models.HandledComment`), and the second lookup only runs
    once the first has found something.

    Never raises. A private reply is a refinement of an ordinary send; a failure
    to look one up must not fail the send itself.
    """
    address = _platform_id(getattr(identity, "platform_user_id", "") or "")
    if not address:
        return None
    try:
        from apps.flows.models import HandledComment
        from apps.flows.triggers import guards

        rows = (
            HandledComment.objects.for_workspace(connection.workspace_id)
            .filter(
                channel_connection=connection,
                commenter_ref=address,
                private_reply_sent_at__isnull=True,
            )
            .order_by("-commented_at")[:5]
        )
        candidate = next((row for row in rows if guards.may_private_reply(row)), None)
        if candidate is None or not _never_messaged_us(connection, identity):
            return None
        return candidate
    except Exception:
        logger.warning("Instagram: could not check for a pending private reply on connection %s.", connection.pk)
    return None


def _never_messaged_us(connection: ChannelConnection, identity: Any) -> bool:
    """Has this contact never sent this account a **message**?

    The question a private reply actually turns on. Meta refuses an ordinary DM
    to somebody who has not written first, and what grants that permission is an
    inbound *message* — not a comment, and not anything we sent them.

    So it is asked of the inbound message rows, and neither of the two nearer
    signals would do. ``last_inbound_at`` is set by a claimed comment as well as
    by a DM, so it answers False for exactly the person who needs a private
    reply. "Have we sent them anything" is a different question again: a contact
    who wrote to us months ago and was never replied to still takes an ordinary
    DM, and contract 1 inserts the row being dispatched *before* calling the
    adapter, so that check also sees the message it is being asked about.

    Only reached once a pending claim has been found, so an established thread
    never pays for it.
    """
    from apps.messaging.models import Message, MessageDirection

    contact_id = getattr(identity, "contact_id", None)
    if contact_id is None:
        return True
    return not (
        Message.objects.for_workspace(connection.workspace_id)
        .filter(
            channel_connection=connection,
            conversation__contact_id=contact_id,
            direction=MessageDirection.IN,
        )
        .exists()
    )


def _spend_private_reply(row: Any, identity: Any, result: dict[str, Any]) -> None:
    """Record that the one private reply this comment gets has been sent.

    ``mark_private_reply_sent`` is L4-A's, and it is the only thing that closes
    the guard — this module adds no second one (the layer brief is explicit).
    The contact is attached here because ``apps.messaging.ingest`` creates none
    for a comment: the identity only exists once the DM thread is open, which is
    the moment this function runs.
    """
    try:
        from apps.flows.triggers import guards

        guards.mark_private_reply_sent(row, contact=getattr(identity, "contact", None))
    except Exception:
        # The message has gone out. Failing the send now would retry it and
        # send a second one, which is the thing the guard exists to prevent.
        logger.exception("Instagram: could not close the private-reply guard on comment %s.", row.pk)
    else:
        logger.info("Instagram: private reply sent for comment row %s (message %s).", row.pk, result.get("message_id"))


def reply_to_comment(token: str, comment_id: str, text: str) -> None:
    """Post a public reply under a comment: ``POST /{ig-comment-id}/replies``."""
    call(token, f"{comment_id}/replies", {"message": text})


def _public_reply_text(config: dict[str, Any]) -> str:
    """SPEC §10's ``public_reply``: none, one fixed text, or one of several."""
    reply = config.get("public_reply")
    if not isinstance(reply, dict):
        return ""
    mode = reply.get("mode")
    texts = [item.strip() for item in (reply.get("texts") or []) if isinstance(item, str) and item.strip()]
    if not texts:
        return ""
    if mode == "static":
        return texts[0]
    if mode == "random":
        # secrets rather than random: not because a public reply needs
        # unpredictability, but because ruff's flake8-bandit rules ban the
        # pseudo-random generator outright and an exemption here would have to
        # be re-justified by every reader.
        return secrets.choice(texts)
    return ""


def _respond_to_comment(context: Any, trigger: Any, row: Any) -> None:
    """L4-A's comment stage just claimed a comment; queue the replies for it.

    **Queued, not inline.** The public reply and the private reply are two Graph
    round trips, and SPEC §7.1 budgets 1.5 s for the whole inline webhook path.
    One INSERT against two TLS handshakes is the same trade
    ``telegram._answer_callback_query`` documents for its callback answer.

    The queue row carries the comment's *text* as well as the row id, because the
    handler re-dispatches the comment through the ingest seam and the trigger's
    keyword rules have to match it there exactly as they matched it here.

    ``open_thread`` is the other thing it carries, and it is false for a
    commenter we already know. The re-dispatch exists only to create the
    contact, the consent record and the messaging window; when all three already
    exist the routing stage has started the flow synchronously and dispatching
    again would start it a second time. The public reply is queued either way —
    an author who configured one wants it for repeat customers too.
    """
    from apps.queueing.registry import schedule as queue_schedule

    try:
        queue_schedule(
            COMMENT_REPLY_ACTION,
            timezone.now(),
            {
                "handled_comment_id": str(row.pk),
                "comment_text": _text(context.event.payload.text, MAX_INBOUND_TEXT_CHARS),
                "parent_comment_id": _text(
                    context.event.payload.extra.get(COMMENT_PARENT_ID_KEY), MAX_PLATFORM_ID_CHARS
                ),
                "open_thread": context.contact is None,
            },
            workspace=context.connection.workspace,
            # One reply per claimed comment, whatever redelivery does upstream.
            idempotency_key=f"ig-comment:{row.pk}",
            max_attempts=3,
        )
    except Exception:
        logger.warning("Instagram: could not queue a reply for comment row %s.", row.pk)


def _answer_comment(payload: dict[str, Any], action: Any) -> None:
    """Run the queued public + private reply for one claimed comment.

    Everything is re-read from the database rather than trusted from the payload:
    the queue row is ours, but the ids in it were derived from an inbound
    webhook, and a handler that took a comment id or a connection from a payload
    would be a way to make the worker post as an arbitrary account.
    """
    row_id = payload.get("handled_comment_id")
    if not isinstance(row_id, str):
        return

    from apps.flows.models import HandledComment
    from apps.flows.triggers import guards

    # Cross-tenant by necessity: a worker drains the whole deployment and has no
    # session workspace. The row it is acting on named this connection.
    row = (
        HandledComment.objects.unscoped()
        .filter(pk=row_id)
        .select_related("channel_connection", "channel_connection__workspace", "trigger")
        .first()
    )
    if row is None or row.trigger is None:
        return
    connection = row.channel_connection
    if connection.platform != Platform.INSTAGRAM.value:
        return
    if row.private_reply_sent_at is not None:
        # A redelivery of an action whose private reply already went out.
        logger.info("Instagram: comment row %s has already been answered.", row.pk)
        return
    if payload.get("open_thread", True) and not guards.may_private_reply(row):
        # Past the seven-day deadline, so there is no thread to open. Checked
        # only on the path that would open one: a commenter we already know has
        # a thread already, and their public reply does not expire with the
        # private-reply window.
        logger.info("Instagram: comment row %s is past its private-reply deadline.", row.pk)
        return

    config = row.trigger.config_json if isinstance(row.trigger.config_json, dict) else {}
    _send_public_reply(connection, row, config)
    if payload.get("open_thread", True):
        _open_thread(connection, row, payload)


def _send_public_reply(connection: ChannelConnection, row: Any, config: dict[str, Any]) -> None:
    """The public reply, if the trigger asks for one. Best effort.

    A failed public reply must not cost the private one — the private reply is
    the thing the flow author actually configured a flow for, and it has a
    seven-day clock on it.

    There is no like here, and there cannot be: Meta's IG Comment reference
    exposes ``like_count`` as read-only and exactly two write operations,
    ``hide`` and the ``replies`` edge. SPEC §10 lists ``like_comment`` in the
    trigger config because Messenger has such an edge; on Instagram the option is
    hidden in the trigger form rather than silently ignored. See
    ``docs/channels/instagram.md``.
    """
    text = _public_reply_text(config)
    if not text:
        return

    from apps.flows.triggers import guards

    if not guards.claim_public_reply(row):
        # Somebody already posted it. The queue's handler contract says a
        # handler "must be safe to run more than once" — zombie recovery re-runs
        # one that committed without being marked done — and a re-run without
        # this claim puts a second visible comment on the customer's post.
        logger.info("Instagram: comment row %s already has its public reply.", row.pk)
        return
    try:
        reply_to_comment(access_token(connection), row.comment_id, text)
    except APIError as exc:
        logger.info("Instagram: public reply to comment row %s was refused (code=%s).", row.pk, exc.code)
    except Exception:
        logger.exception("Instagram: public reply failed for comment row %s.", row.pk)


def _open_thread(connection: ChannelConnection, row: Any, payload: dict[str, Any]) -> None:
    """Hand the claimed comment back to the inbound pipeline so a flow can run.

    This is the step that makes comment-to-DM work at all, and it is worth
    saying why it is a re-dispatch rather than four direct calls.

    ``apps.messaging.ingest`` deliberately creates no contact for a comment — one
    viral post would otherwise be a contact-spam amplifier — and it is also the
    single write site for ``identity.window_expires_at`` (ROADMAP contract 3,
    enforced by an AST scan). So the contact, the consent record and the
    messaging window that lets the reply through the compliance engine can only
    be created *there*. The marker below is what tells it this particular comment
    has already been claimed by SPEC §10's once-per-comment guard, which is what
    makes creating one identity for it safe.

    Routing then matches the comment trigger a second time — with a contact in
    hand this time — and starts the flow, so the flow's first message *is* the
    private reply, exactly as SPEC §10 words it. The text is carried through the
    queue row precisely so that second match sees what the first one saw.
    """
    event = NormalizedEvent(
        type=EventType.COMMENT,
        connection=connection,
        platform_user_id=row.commenter_ref,
        provider_event_id=f"ig:pr:{row.pk}",
        timestamp=row.commented_at,
        payload=EventPayload(
            text=_text(payload.get("comment_text"), MAX_INBOUND_TEXT_CHARS),
            comment_id=row.comment_id,
            extra={
                PRIVATE_REPLY_CLAIMED_KEY: True,
                COMMENT_POST_ID_KEY: row.post_id,
                COMMENT_PARENT_ID_KEY: _text(payload.get("parent_comment_id"), MAX_PLATFORM_ID_CHARS),
            },
        ),
    )
    channels_ingest.process_events(connection, (event,))


# ---------------------------------------------------------------------------
# The post picker (SPEC §10's comment trigger config)
# ---------------------------------------------------------------------------

#: How many recent posts the picker offers. Meta pages ``/me/media``; one page is
#: what a person scans, and a trigger scoped to a post from last year is
#: configured by pasting its id.
MEDIA_PAGE_SIZE = 24

#: Fields the picker needs and nothing more. Every one of them is
#: attacker-controlled in the sense that matters — the caption is whatever the
#: account posted — so the template escapes them like any other platform string.
MEDIA_FIELDS = "id,caption,media_type,media_url,thumbnail_url,permalink,timestamp"


def recent_media(connection: ChannelConnection, *, limit: int = MEDIA_PAGE_SIZE) -> list[dict[str, str]]:
    """Recent posts on this account, for the comment trigger's post picker.

    Returns plain dictionaries of bounded strings rather than the Graph payload:
    the caller is a template, and handing a view the provider's json invites
    somebody to render a key nobody vetted.
    """
    body = call(
        access_token(connection),
        "me/media",
        method="GET",
        params={"fields": MEDIA_FIELDS, "limit": max(1, min(limit, MEDIA_PAGE_SIZE))},
        timeout=BACKGROUND_TIMEOUT,
    )
    raw = body.get("data")
    if not isinstance(raw, list):
        return []
    posts: list[dict[str, str]] = []
    for item in raw[:limit]:
        if not isinstance(item, dict):
            continue
        post_id = _platform_id(item.get("id"))
        if not post_id:
            continue
        thumbnail = _text(item.get("thumbnail_url"), MAX_URL_CHARS) or _text(item.get("media_url"), MAX_URL_CHARS)
        posts.append(
            {
                "id": post_id,
                "caption": _text(item.get("caption"), MAX_EXTRA_CHARS),
                "media_type": _text(item.get("media_type"), 40),
                "thumbnail_url": thumbnail,
                "permalink": _text(item.get("permalink"), MAX_URL_CHARS),
                "timestamp": _text(item.get("timestamp"), 40),
            }
        )
    return posts


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

register_handler(COMMENT_REPLY_ACTION)(_answer_comment)

register_responder(
    Platform.INSTAGRAM.value,
    CommentResponder(
        respond=_respond_to_comment,
        # Meta publishes no way to like an Instagram comment: the IG Comment
        # reference has ``like_count`` as read-only and only ``hide`` and
        # ``replies`` as write operations. Saying so as data is what lets the
        # trigger form hide the option for Instagram while leaving it available
        # to a platform whose API has one.
        supports_like=False,
        picker_route="channels:instagram_posts",
    ),
)

register_adapter(Platform.INSTAGRAM, InstagramAdapter)
