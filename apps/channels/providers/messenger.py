"""Facebook Messenger adapter (SPEC §6.4) — issue #18.

Written from ``apps.channels.providers.telegram``, whose docstring says outright
that a Layer-5 author "should be able to replace the helpers and keep the class".
That is what happened here: the class below is the same six methods against the
same SPEC §6.1 interface, and everything above it is Messenger's own vocabulary —
the ``page`` delivery shape, the Send API envelope, the generic template, the
20-character button title, the message tags.

What is *not* re-implemented, because Layer 4 already owns it:

* HTTP mechanics, the timeout policy, ``429`` → :class:`RateLimitError` and the
  "never put a URL in an error message" rule — ``providers.base.request_json``,
  reached through :mod:`apps.channels.providers.meta_common`;
* block downgrading — :func:`apps.channels.downgrade.downgrade`, shared;
* the signature scheme, the Graph client, credential resolution and the
  needs-reauth transition — :mod:`~apps.channels.providers.meta_common`, which
  Instagram (#17) shares;
* the compliance decision. ``apps.messaging.compliance`` reads Messenger's
  ``PlatformPolicy`` row as **data** and hands this module a message whose ``tag``
  is already decided. There is no Messenger branch anywhere in ``apps/messaging/``
  and this module must never need one.

--------------------------------------------------------------------------
Rate limits, and why there is no throttle in this file
--------------------------------------------------------------------------

Meta's Send API limit scales with the page's audience and answers with a ``613``
or a ``429`` when exceeded. Neither needs code here.

The **global** limit is the connection's token bucket, already implemented in
``apps.messaging.buckets`` and configured by ``rate_default=40.0`` in
:mod:`apps.channels.policy`. The **per-recipient** limit is satisfied by the shape
of the system rather than by a timer: SPEC §9.6 serialises everything a contact
does behind one advisory lock and SPEC §9.2 allows one live execution per contact,
so two messages to the same person cannot be in flight at once. A second throttle
here would be a sleep held *inside* that lock. When Meta disagrees anyway it says
so with a 429 and the send pipeline reschedules (``apps.messaging.services._defer``).

--------------------------------------------------------------------------
Message tags
--------------------------------------------------------------------------

SPEC §6.4 and §8: outside the 24-hour window, automation and broadcasts need one
of the three non-promotional tags, and ``HUMAN_AGENT`` extends an **agent** send to
seven days. All of that is decided before this module is called —
``compliance.Allowed.apply`` *replaces* ``outbound.tag`` rather than passing the
caller's through, which is what makes SPEC §22's "available only to inbox sends,
never automation, hard-coded" true no matter what a flow author types.

So this module's job is one mapping: a tag present means
``messaging_type=MESSAGE_TAG`` plus that tag, and its absence means
``messaging_type=RESPONSE``. It additionally refuses a tag outside
``capabilities.tags_supported`` — read from the registry, not restated — which is
defence in depth rather than a second policy: Meta disables pages over a
promotional message sent under a tag, so a bug upstream should fail the send
visibly here instead of reaching the platform.

--------------------------------------------------------------------------
Secrets
--------------------------------------------------------------------------

The page access token *is* the page. It lives encrypted in
``connection.credentials`` and is only ever sent in an ``Authorization`` header,
never in a URL (see :mod:`~apps.channels.providers.meta_common`). The app secret
is never held by this module at all — it is resolved per call from SPEC §4's
credential chain. ``apps.common.logging`` scrubs both the ``Bearer`` form and
Meta's ``EAA…`` token shape (SECURITY-BASELINE §5).
"""

import logging
import secrets
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

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
from apps.channels.models import ChannelConnection
from apps.channels.posts import PostListingError, clean_post, register_post_lister
from apps.channels.providers import meta_common
from apps.channels.providers.base import BACKGROUND_TIMEOUT, Adapter
from apps.channels.providers.exceptions import APIError
from apps.channels.registry import register_adapter
from apps.common.platforms import Platform
from apps.queueing.registry import register_handler
from apps.queueing.registry import schedule as queue_schedule

if TYPE_CHECKING:
    from django.http import HttpRequest

logger = logging.getLogger(__name__)

__all__ = [
    "COMMENT_ACTION",
    "COMMENT_DM_ACTION",
    "GET_STARTED_PAYLOAD",
    "SUBSCRIBED_FIELDS",
    "MessengerAdapter",
    "send_body",
    "set_get_started",
    "subscribe_page",
    "unsubscribe_page",
    "wire_calls",
]

#: The webhook ``object`` Messenger deliveries carry (SPEC §6.4). A delivery for
#: any other object was not addressed to this adapter — an Instagram batch that
#: reached the Messenger URL, say — and produces no events rather than being
#: parsed on the assumption that the shapes are close enough.
WEBHOOK_OBJECT = "page"

#: The fields a page is subscribed to on connect (SPEC §6.4, issue #18). An
#: allowlist rather than everything, because each unsubscribed field is a
#: delivery that would cost a request, a dedup row and a parse to discard.
SUBSCRIBED_FIELDS: tuple[str, ...] = (
    "messages",
    "messaging_postbacks",
    "messaging_referrals",
    "message_deliveries",
    "message_reads",
    # SPEC §10's comment trigger. Page posts, comments and likes all arrive here.
    "feed",
)

#: The Get Started button's payload, configured at connect time. It is one of the
#: three spellings ``apps.flows.triggers.matching.WELCOME_POSTBACKS`` already
#: recognises, so SPEC §10's welcome trigger needs no Messenger-specific matcher.
GET_STARTED_PAYLOAD = "GET_STARTED"

#: The queued actions that answer a claimed comment. Registered at the foot of
#: this module, beside the adapter.
#:
#: **Two, not one, and the split is the whole design.** The public half posts a
#: comment and a like — neither of which the platform lets us make idempotent, so
#: a retry would put a second reply under a customer's comment. The DM half is
#: idempotent by construction (the claim guard, ``persist_events``' dedup and SPEC
#: §9.2's one-execution-per-contact rule all hold), and it is the half worth
#: retrying, because it is the half the customer was promised.
#:
#: So they get opposite retry policies — see :func:`enqueue_comment_actions` — and
#: a failure in either cannot cost the other.
COMMENT_ACTION = "messenger_comment_actions"
COMMENT_DM_ACTION = "messenger_comment_dm"

_CAPABILITIES: Capabilities = capabilities_for(Platform.MESSENGER)

#: Read from the capability table rather than restated, so the two cannot drift.
MAX_TEXT_CHARS = _CAPABILITIES.max_text_len
MAX_BUTTONS = _CAPABILITIES.max_buttons
MAX_QUICK_REPLIES = _CAPABILITIES.max_quick_replies

#: Tags Meta accepts on this platform. The registry's list, again — the adapter
#: reads the capability table, it never patches it (ROADMAP contract 4).
ALLOWED_TAGS: frozenset[str] = frozenset(_CAPABILITIES.tags_supported)

# Meta limits with no field on :class:`Capabilities`, because the flow builder
# does not warn about them: they are adapter constants, each named for what it
# bounds. Every one of these is enforced by Meta at send time, so a value over
# the cap is a rejected message rather than a truncated one.
MAX_BUTTON_TITLE_CHARS = 20
MAX_QUICK_REPLY_TITLE_CHARS = 20
MAX_CARD_TITLE_CHARS = 80
MAX_CARD_SUBTITLE_CHARS = 80
MAX_TEMPLATE_ELEMENTS = 10
MAX_BUTTON_TEMPLATE_TEXT_CHARS = 640
MAX_PAYLOAD_BYTES = 1000

#: Longest inbound text carried out of a parse. ``apps.messaging.ingest`` bounds
#: it again downstream; this one exists so a hostile payload cannot make us hold
#: an arbitrarily long string in the first place (SECURITY-BASELINE §§2, 7).
MAX_INBOUND_TEXT_CHARS = 10_000

#: Longest attacker-supplied display string kept in ``payload.extra``.
MAX_EXTRA_CHARS = 200

#: An m.me ``?ref=`` payload, bounded to what a ``ref_url`` trigger can hold.
#: The same 64 that ``apps.flows.triggers.types.MAX_REF_CHARS`` fixes, declared
#: locally for the reason ``telegram.MAX_REF_CHARS`` is: ``apps.channels`` must
#: not import ``apps.flows`` at module scope, and a test pins the two equal.
MAX_REF_CHARS = 64

#: How many attachment URLs one inbound message may carry out of a parse.
MAX_ATTACHMENTS = 10
MAX_ATTACHMENT_URL_CHARS = 2000

# Where a comment event carries its post and its parent. The contract is
# ``apps.flows.triggers.types``' — ``COMMENT_POST_ID_KEY`` and
# ``COMMENT_PARENT_ID_KEY`` — duplicated here as literals rather than imported,
# following ``apps.flows.triggers.pipeline.ROUTING_PROCESSOR``: ``apps.channels``
# has no module-scope dependency on ``apps.flows`` and this is not the place to
# introduce one. ``test_messenger_comments.py`` pins them equal, so the
# duplication cannot drift silently.
COMMENT_POST_ID_KEY = "post_id"
COMMENT_PARENT_ID_KEY = "parent_comment_id"
COMMENT_TEXT_KEY = "comment_text"

#: How many recent outbound messages a read receipt may walk back over. Meta's
#: read receipts carry a **watermark** rather than message ids, so resolving one
#: means asking which of our own messages it covers; the bound is what keeps a
#: forged watermark from turning into an unbounded scan.
MAX_READ_RECEIPT_MESSAGES = 25

#: How many message ids one ``message_deliveries`` event may carry.
#:
#: Its own constant, not the read-receipt one. They bound different mechanisms —
#: this is "how many ids did Meta name", that is "how far back does a watermark
#: reach" — and sharing one would mean tuning either silently moved the other.
#: Generous, because a receipt past the cap is a message stuck at ``sent``
#: forever: Meta does not resend a delivery receipt.
MAX_DELIVERY_MIDS = 100

#: How many read receipts in **one delivery** are resolved at all.
#:
#: Resolving a watermark costs two indexed queries, and a delivery may legally
#: carry up to ``meta_common.MAX_ENTRIES`` entries — so without this, one signed
#: batch could put a couple of hundred queries inside SPEC §7.1's 1.5-second ack
#: budget. Real batches carry a handful. Past the cap the receipts are dropped
#: with a log line rather than the delivery being refused: a read receipt is
#: bookkeeping, and losing one costs a "Read" marker in the inbox, while a slow
#: ack costs every message in the batch.
MAX_READ_RECEIPTS_PER_DELIVERY = 5

#: Meta error codes that mean "never send to this person again". 551 is "this
#: person isn't available right now" — blocked the page, deleted the account, or
#: the thread is gone. All three mean the same thing operationally, and
#: continuing to try is what gets a page restricted.
USER_UNAVAILABLE_CODES = frozenset({"551", "2018108"})

#: Attachment kinds Meta renders natively, and the ``attachment.type`` each
#: block kind becomes. Anything outside this map has already been turned into
#: text by the downgrade renderer.
_ATTACHMENT_TYPES: dict[str, str] = {
    "image": "image",
    "audio": "audio",
    "video": "video",
    "file": "file",
}

#: ``attachment.type`` values on an inbound message whose payload carries a URL
#: worth keeping. ``fallback`` is a shared link, ``template`` is a card the
#: person forwarded; both have URLs and neither is media we can name.
_INBOUND_ATTACHMENT_TYPES = frozenset({"image", "audio", "video", "file", "fallback"})


# ---------------------------------------------------------------------------
# Graph calls this adapter makes
# ---------------------------------------------------------------------------


def send_body(connection: ChannelConnection, body: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
    """One ``POST /<page_id>/messages``. Returns Meta's response body.

    The single outbound call shape: everything this adapter sends — a message, a
    typing indicator, a read receipt — is this endpoint with a different body.
    """
    return meta_common.graph_call(
        meta_common.page_token(connection),
        "POST",
        f"{connection.external_id}/messages",
        json=body,
        timeout=timeout,
    )


def subscribe_page(connection: ChannelConnection, *, fields: tuple[str, ...] = SUBSCRIBED_FIELDS) -> None:
    """Subscribe the app to this page's webhook fields (SPEC §6.4).

    Called at connect time and nowhere else. Without it a page is connected,
    looks healthy, and never delivers anything — which is the failure
    ``views_telegram`` avoids by rolling its connection back when ``setWebhook``
    fails, and which ``views_messenger`` avoids the same way.
    """
    meta_common.graph_call(
        meta_common.page_token(connection),
        "POST",
        f"{connection.external_id}/subscribed_apps",
        params={"subscribed_fields": ",".join(fields)},
        timeout=BACKGROUND_TIMEOUT,
    )


def unsubscribe_page(connection: ChannelConnection) -> None:
    """Stop Meta delivering for this page. Called when a connection is removed."""
    meta_common.graph_call(
        meta_common.page_token(connection),
        "DELETE",
        f"{connection.external_id}/subscribed_apps",
        timeout=BACKGROUND_TIMEOUT,
    )


def set_get_started(connection: ChannelConnection) -> None:
    """Configure the Get Started button so the welcome trigger can fire.

    SPEC §10 maps Messenger's welcome trigger to the ``get_started`` postback, and
    Messenger only *sends* that postback if the page has a Get Started button
    configured. So this is not decoration: without it the welcome trigger is a row
    that can never match, and nothing in the product would say why.
    """
    meta_common.graph_call(
        meta_common.page_token(connection),
        "POST",
        f"{connection.external_id}/messenger_profile",
        json={"get_started": {"payload": GET_STARTED_PAYLOAD}},
        timeout=BACKGROUND_TIMEOUT,
    )


# ---------------------------------------------------------------------------
# Outbound: OutboundMessage -> Send API bodies
# ---------------------------------------------------------------------------


def wire_calls(recipient: dict[str, Any], message: OutboundMessage, *, tag: str | None = None) -> list[dict[str, Any]]:
    """Send API bodies for one already-downgraded message.

    Pure: no HTTP, no database, no clock. That is what lets the payload snapshots
    be a table, and what lets a reader check a body against Meta's documentation
    without reading the send loop.

    ``message`` is expected to have been through
    :func:`apps.channels.downgrade.downgrade` already, so text is within the 2000
    cap and the button and quick-reply counts are within Meta's. Cards and
    galleries survive that pass — Messenger renders them natively — and become
    generic templates here.
    """
    calls: list[dict[str, Any]] = []
    for block in message.blocks:
        calls.extend(_block_messages(block))
    if not calls:
        return []

    calls = _apply_buttons(calls, message)
    _apply_quick_replies(calls, message)

    envelope = _envelope(recipient, tag)
    return [{**envelope, "message": call} for call in calls]


def _envelope(recipient: dict[str, Any], tag: str | None) -> dict[str, Any]:
    """The Send API fields that wrap every message body.

    SPEC §6.4: ``messaging_type`` is ``RESPONSE`` inside the window and
    ``MESSAGE_TAG`` plus the tag outside it. The tag was decided by
    ``apps.messaging.compliance``; this only spells it on the wire.
    """
    if tag:
        return {"recipient": recipient, "messaging_type": "MESSAGE_TAG", "tag": tag}
    return {"recipient": recipient, "messaging_type": "RESPONSE"}


def _block_messages(block: Any) -> list[dict[str, Any]]:
    if isinstance(block, TextBlock):
        return _text_messages(block.text)
    if isinstance(block, MediaBlock):
        return _media_messages(block)
    if isinstance(block, CardBlock):
        return _template_messages([block.card])
    if isinstance(block, GalleryBlock):
        return _template_messages(list(block.cards))
    return []


def _text_messages(text: str) -> list[dict[str, Any]]:
    """``{"text": ...}`` bodies, split if the text is over the cap.

    Meta rejects an empty ``text`` outright, so a blank block produces no message
    at all rather than one that fails at the platform.
    """
    if not text.strip():
        return []
    return [{"text": part} for part in split_text(text, MAX_TEXT_CHARS)]


def _media_messages(block: MediaBlock) -> list[dict[str, Any]]:
    """An attachment, plus a text message for its caption.

    Meta's attachment payload has no caption field — unlike Telegram's, which has
    one with a lower cap — so a caption travels as its own message afterwards.
    That keeps all of it, in order, which is what a person would do.

    ``is_reusable`` is deliberately **not** set. It tells Meta to keep a copy and
    hand back an attachment id, which is a caching optimisation for a media
    library we address by URL anyway, and it makes every send of a contact-facing
    asset leave a durable copy on Meta's side.
    """
    attachment_type = _ATTACHMENT_TYPES.get(block.kind)
    if attachment_type is None or not block.url:
        # An unsupported kind should already have become text upstream, and a
        # media block with no address cannot be sent at all. Either way the
        # caption and the URL are the only things left worth delivering.
        return _text_messages("\n".join(part for part in (block.caption, block.url) if part))

    messages: list[dict[str, Any]] = [
        {"attachment": {"type": attachment_type, "payload": {"url": block.url}}},
    ]
    messages.extend(_text_messages(block.caption))
    return messages


def _template_messages(cards: list[Card]) -> list[dict[str, Any]]:
    """A generic template — Messenger's card and carousel (SPEC §6.4).

    Meta caps a carousel at ten elements, so a longer gallery becomes several
    templates rather than being cut. An element needs a title; a card without one
    falls back to its subtitle, and a card with neither is dropped, because Meta
    rejects the whole template over one titleless element.
    """
    elements = [element for element in (_element(card) for card in cards) if element is not None]
    if not elements:
        return []
    return [
        {
            "attachment": {
                "type": "template",
                "payload": {"template_type": "generic", "elements": elements[index : index + MAX_TEMPLATE_ELEMENTS]},
            }
        }
        for index in range(0, len(elements), MAX_TEMPLATE_ELEMENTS)
    ]


def _element(card: Card) -> dict[str, Any] | None:
    title = (card.title or card.subtitle).strip()[:MAX_CARD_TITLE_CHARS]
    if not title:
        return None
    element: dict[str, Any] = {"title": title}
    if card.subtitle and card.subtitle.strip() != title:
        element["subtitle"] = card.subtitle.strip()[:MAX_CARD_SUBTITLE_CHARS]
    if card.image_url:
        element["image_url"] = card.image_url
    if card.url:
        element["default_action"] = {"type": "web_url", "url": card.url}
    buttons = [button for button in (_button(item, "") for item in card.buttons[:MAX_BUTTONS]) if button]
    if buttons:
        element["buttons"] = buttons
    return element


def _apply_buttons(calls: list[dict[str, Any]], message: OutboundMessage) -> list[dict[str, Any]]:
    """Attach ``message.buttons`` to the last message, in whichever form fits.

    Meta has no equivalent of Telegram's ``reply_markup``: buttons ride *inside* a
    template, so a text message with buttons is a different message type — a
    button template — rather than the same message with a keyboard bolted on.

    Three cases, in order of fidelity:

    1. The last message is plain text → it becomes a **button template**. Meta
       caps that template's text at 640 characters against 2000 for a plain
       message, so an over-long final chunk is split and only the tail carries the
       buttons.
    2. The last message is an attachment or a template → the buttons become
       **quick replies** instead. A :class:`~apps.channels.events.QuickReply`
       comes back as ``EventPayload.button_id`` exactly like a postback button
       does, so the semantics survive the change of clothes; this is the same
       trade ``telegram._reply_markup`` makes in the opposite direction.
    3. Neither fits → the buttons are left out, loudly. Visible beats a body Meta
       would reject.
    """
    buttons = [
        button for button in (_button(item, message.node_id) for item in message.buttons[:MAX_BUTTONS]) if button
    ]
    if not buttons:
        return calls

    last = calls[-1]
    text = last.get("text")
    if isinstance(text, str):
        head, tail = _split_for_button_template(text)
        template = {
            "attachment": {
                "type": "template",
                "payload": {"template_type": "button", "text": tail, "buttons": buttons},
            }
        }
        return [*calls[:-1], *([{"text": head}] if head else []), template]

    room = MAX_QUICK_REPLIES - len(message.quick_replies)
    if room > 0:
        logger.debug(
            "Messenger: %s button(s) sent as quick replies because the message ends in an attachment.",
            min(len(buttons), room),
        )
        chips = [chip for chip in (_quick_reply_from_button(item, message.node_id) for item in message.buttons) if chip]
        if chips:
            last.setdefault("quick_replies", []).extend(chips[:room])
            return calls
        # Every button was a URL button, and a quick reply cannot open a link. A
        # chip that does nothing when tapped is worse than no chip, so they are
        # dropped — and the key is not left behind empty, because Meta rejects an
        # empty ``quick_replies`` array.
        logger.debug("Messenger: URL buttons after an attachment cannot be represented; they were left out.")
        return calls

    logger.warning("Messenger: %s button(s) could not be represented and were left out.", len(buttons))
    return calls


def _split_for_button_template(text: str) -> tuple[str, str]:
    """``(what goes before, what carries the buttons)`` for a button template."""
    if len(text) <= MAX_BUTTON_TEMPLATE_TEXT_CHARS:
        return "", text
    return text[:-MAX_BUTTON_TEMPLATE_TEXT_CHARS], text[-MAX_BUTTON_TEMPLATE_TEXT_CHARS:]


def _apply_quick_replies(calls: list[dict[str, Any]], message: OutboundMessage) -> None:
    """Put quick replies on the last message, so they arrive with the final bubble."""
    chips = [chip for chip in (_quick_reply(item, message.node_id) for item in message.quick_replies) if chip]
    if not chips:
        return
    existing = calls[-1].setdefault("quick_replies", [])
    room = MAX_QUICK_REPLIES - len(existing)
    existing.extend(chips[: max(room, 0)])
    if not existing:
        calls[-1].pop("quick_replies", None)


def _button(button: Button, node_id: str) -> dict[str, Any] | None:
    """One template button, or None when it cannot be represented."""
    title = _title(button, MAX_BUTTON_TITLE_CHARS)
    if not title:
        return None
    if button.is_url:
        return {"type": "web_url", "url": button.url, "title": title}
    payload = _payload(node_id, button.id)
    if payload is None:
        return None
    return {"type": "postback", "title": title, "payload": payload}


def _quick_reply(item: QuickReply, node_id: str) -> dict[str, Any] | None:
    title = _title(item, MAX_QUICK_REPLY_TITLE_CHARS)
    payload = _payload(node_id, item.id)
    if not title or payload is None:
        return None
    return {"content_type": "text", "title": title, "payload": payload}


def _quick_reply_from_button(button: Button, node_id: str) -> dict[str, Any] | None:
    """A postback button wearing a quick reply's clothes. See :func:`_apply_buttons`.

    A URL button has nowhere to go here — a quick reply cannot open a link — so it
    is dropped rather than turned into a chip that does nothing when tapped.
    """
    if button.is_url:
        return None
    return _quick_reply(QuickReply(id=button.id, label=button.label), node_id)


def _title(item: Button | QuickReply, limit: int) -> str:
    """Meta rejects an empty title; fall back to the id, then bound it."""
    return (item.label or item.id).strip()[:limit]


def _payload(node_id: str, button_id: str) -> str | None:
    """SPEC §6.2's ``node_id:button_id``, within Meta's 1000-byte payload cap.

    The same encoding Telegram uses, and for the same reason: the engine matches a
    press on the *button* id against the waiting node's handles, and the node id
    is decoration that helps a human reading a log.

    **The separator is always present**, even with no node — a message from the
    inbox or the public API has none, and it encodes as ``:<id>`` — so
    :func:`_button_id` can split on the first colon and be right every time. A
    button id may legitimately contain a colon; the node id never can
    (``apps.flows.schema.handles.HANDLE_PATTERN``).

    None means the button cannot be represented and is left out of the message
    entirely, which is visible, rather than sent with a payload that would come
    back unmatchable.
    """
    if not button_id:
        return None
    combined = f"{node_id}:{button_id}"
    if len(combined.encode("utf-8")) <= MAX_PAYLOAD_BYTES:
        return combined
    bare = f":{button_id}"
    if len(bare.encode("utf-8")) <= MAX_PAYLOAD_BYTES:
        logger.debug("Messenger: payload for %r dropped its node prefix to fit %s bytes.", button_id, MAX_PAYLOAD_BYTES)
        return bare
    logger.warning("Messenger: a button id is over %s bytes and was left out.", MAX_PAYLOAD_BYTES)
    return None


def _button_id(payload: str) -> str:
    """The button half of a postback payload.

    Split on the **first** colon, because that is where :func:`_payload` put it.
    A payload with no colon is accepted as a bare button id: Meta's own
    ``GET_STARTED`` has that shape, and so does anything an operator configured in
    the page's persistent menu by hand.
    """
    return payload.split(":", 1)[-1]


# ---------------------------------------------------------------------------
# Inbound: a `page` delivery -> NormalizedEvents
# ---------------------------------------------------------------------------


def _text(value: Any, limit: int = MAX_INBOUND_TEXT_CHARS) -> str:
    """A bounded string, or "". Every inbound field goes through this."""
    return meta_common.text_value(value, limit)


def _epoch_ms(raw: Any) -> datetime | None:
    """A Meta millisecond timestamp as a datetime, or None if it is not one.

    The strict half of :func:`_timestamp`, split out because two callers need to
    tell "the platform told us when" from "we had to guess". ``fromtimestamp``
    raises on values outside the platform's range and the division itself raises on
    an integer too large to be a float — both are exactly what a hostile payload
    sends, and both are caught.
    """
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(raw / 1000, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _timestamp(raw: Any, fallback: Any = None) -> datetime:
    """A Meta millisecond timestamp as a datetime, or now.

    A wrong clock on an event is a cosmetic problem; refusing the event because
    its ``timestamp`` was a string is a lost message.

    Callers that put the result in a **deduplication key** must not use this —
    see :func:`_event_clock`, which is what those use instead.
    """
    for candidate in (raw, fallback):
        parsed = _epoch_ms(candidate)
        if parsed is not None:
            return parsed
    return timezone.now()


def _event_clock(raw: Any, fallback: Any = None) -> str:
    """The platform's own time as a stable string, or "" when it did not give one.

    Meta sends no id for a postback or a referral, so the id is derived from the
    event's content — and the content has to include the time, or two genuine
    presses of the same button collide into one and the second vanishes.

    The whole point of the empty string is the case :func:`_timestamp` papers
    over. If a delivery's timestamps are unreadable, ``_timestamp`` falls back to
    ``timezone.now()``, and hashing *that* would give the same event a different id
    on every redelivery — so Meta's retry would be processed a second time, firing
    the welcome or ref_url flow twice. Omitting the clock instead makes the id
    depend only on values the platform actually sent, which is what a redelivery
    reproduces exactly.
    """
    for candidate in (raw, fallback):
        parsed = _epoch_ms(candidate)
        if parsed is not None:
            return parsed.isoformat()
    return ""


def _sender_extra(item: dict[str, Any]) -> dict[str, Any]:
    """Display detail worth keeping. Attacker-controlled: escape on render."""
    extra: dict[str, Any] = {}
    recipient = item.get("recipient")
    if isinstance(recipient, dict):
        page_id = meta_common.bounded_id(recipient.get("id"))
        if page_id:
            extra["page_id"] = page_id
    return extra


def _attachment_urls(message: dict[str, Any]) -> tuple[str, ...]:
    """URLs of whatever this inbound message carries.

    ``EventPayload.attachments`` is documented as URLs, which is what Meta sends —
    unlike Telegram's ``file_id``, which is why that adapter uses ``media_ids``
    instead. **Recorded, never fetched**: SECURITY-BASELINE §6 puts every
    server-side fetch of a platform-supplied URL behind
    ``apps.common.outbound.guarded_request``, and nothing here needs the bytes.
    """
    raw = message.get("attachments")
    if not isinstance(raw, list):
        return ()
    urls: list[str] = []
    for attachment in raw[:MAX_ATTACHMENTS]:
        if not isinstance(attachment, dict):
            continue
        kind = attachment.get("type")
        # ``isinstance`` before the membership test, not for tidiness: ``in`` on a
        # frozenset hashes its argument, and a payload whose ``type`` is a list
        # would raise TypeError out of a parser whose whole contract is that it
        # never does (SECURITY-BASELINE §2).
        if not isinstance(kind, str) or kind not in _INBOUND_ATTACHMENT_TYPES:
            continue
        payload = attachment.get("payload")
        url = _text(payload.get("url"), MAX_ATTACHMENT_URL_CHARS) if isinstance(payload, dict) else ""
        if url:
            urls.append(url)
    return tuple(urls)


def _referral_ref(referral: Any) -> str:
    """The ``ref`` of an m.me link, bounded to what a ref_url trigger holds."""
    if not isinstance(referral, dict):
        return ""
    return _text(referral.get("ref"), MAX_REF_CHARS)


def _referral_extra(referral: Any) -> dict[str, Any]:
    if not isinstance(referral, dict):
        return {}
    extra: dict[str, Any] = {}
    for key in ("source", "type", "ad_id"):
        value = _text(referral.get(key), MAX_EXTRA_CHARS)
        if value:
            extra[key] = value
    return extra


# ---------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------


class MessengerAdapter(Adapter):
    """SPEC §6.4, implemented against SPEC §6.1's interface."""

    platform = Platform.MESSENGER.value
    capabilities = _CAPABILITIES
    webhook_content = "json"

    #: How many more read receipts this delivery may resolve. Reset by every
    #: :meth:`parse_events`; see :data:`MAX_READ_RECEIPTS_PER_DELIVERY`.
    _read_receipt_budget: int = MAX_READ_RECEIPTS_PER_DELIVERY

    # -- inbound ------------------------------------------------------------

    def resolve_connection(self, request: "HttpRequest", raw_body: bytes) -> ChannelConnection | None:
        """Which page this delivery is for, from the ids inside it.

        SPEC §7.1 gives Meta one ``/webhooks/messenger/`` URL per deployment with
        "connection resolved from payload ids", so this reads the first entry's
        page id out of a body that has **not** been verified yet. That ordering is
        inherent to the specification — the signing key is the app secret, which is
        resolved from the connection's workspace — and ``views_webhooks._ingest``
        is written around it: the JSON nesting cap is a byte scan that runs first,
        so the parser reached here is a bounded one.

        The endpoint checks the platform and the status of whatever comes back
        before trusting it (``views_webhooks._usable``), and the signature is then
        verified against *that* row's app secret.
        """
        payload = security.json_payload(request) or {}
        if payload.get("object") != WEBHOOK_OBJECT:
            return None
        for entry in meta_common.entries(payload):
            connection = meta_common.resolve_by_page_id(self.platform, meta_common.bounded_id(entry.get("id")))
            if connection is not None:
                return connection
        return None

    def verify_webhook(self, request: "HttpRequest", connection: ChannelConnection) -> bool:
        """Meta's ``X-Hub-Signature-256`` over the raw body (SPEC §6.4).

        The key is the **app** secret from SPEC §4's credential chain, not
        ``connection.webhook_secret`` — Meta signs with the app the page is
        subscribed to, and there is no per-connection secret to present. Which is
        also why :meth:`on_webhook_secret_rotated` has nothing to do.
        """
        return meta_common.verify_signature(request, connection)

    def parse_events(self, request: "HttpRequest", connection: ChannelConnection) -> list[NormalizedEvent]:
        """One ``page`` delivery becomes zero or more normalized events.

        Defensive by contract (SECURITY-BASELINE §2): every value here was typed
        by a stranger. Nothing raises, nothing assumes a key exists, everything is
        length-bounded, and an item we do not understand produces no event rather
        than a half-populated one.

        Each entry resolves **its own** connection. One Meta delivery legitimately
        spans several pages, and ``views_webhooks._record`` groups a batch by each
        event's own connection precisely so page B's messages are not logged and
        dispatched as page A's.
        """
        payload = security.json_payload(request) or {}
        if payload.get("object") != WEBHOOK_OBJECT:
            logger.info("A delivery on connection %s was not a page object; ignoring it.", connection.pk)
            return []

        # Reset per delivery. The registry hands out a fresh adapter instance per
        # use, so this is belt and braces — but ``parse_events`` is a public
        # method and a caller that reused an instance would otherwise inherit a
        # spent budget and silently drop receipts.
        self._read_receipt_budget = MAX_READ_RECEIPTS_PER_DELIVERY

        entries = meta_common.entries(payload)
        # One query for the whole batch, not one per entry. A delivery may carry
        # up to ``meta_common.MAX_ENTRIES`` entries, and resolving them
        # individually put that many round trips inside SPEC §7.1's 1.5-second
        # budget — on the path the layer's own ack-latency test holds to 500 ms.
        owners = meta_common.resolve_by_page_ids(
            self.platform,
            (
                page_id
                for page_id in (meta_common.bounded_id(entry.get("id")) for entry in entries)
                if page_id and page_id != connection.external_id
            ),
        )

        events: list[NormalizedEvent] = []
        for entry in entries:
            owner = self._entry_connection(entry, connection, owners)
            if owner is None:
                continue
            events.extend(self._from_entry(owner, entry))
        return events

    def _entry_connection(
        self,
        entry: dict[str, Any],
        verified: ChannelConnection,
        owners: dict[str, ChannelConnection],
    ) -> ChannelConnection | None:
        """The connection one ``entry`` belongs to, or None to drop it.

        An entry naming the verified page — or naming no page at all — is the
        verified connection. An entry naming another page is looked up in ``owners``,
        the batch resolved once by the caller, and a page this deployment does not
        hold is dropped: the signature proves the sender holds the app secret, not
        that every id in the body is ours to write.
        """
        page_id = meta_common.bounded_id(entry.get("id"))
        if not page_id or page_id == verified.external_id:
            return verified
        owner = owners.get(page_id)
        if owner is None:
            logger.info("Dropped a Messenger entry for a page this deployment does not hold.")
        return owner

    def _from_entry(self, connection: ChannelConnection, entry: dict[str, Any]) -> list[NormalizedEvent]:
        entry_time = entry.get("time")
        events: list[NormalizedEvent] = []

        messaging = entry.get("messaging")
        if isinstance(messaging, list):
            for item in messaging:
                if isinstance(item, dict):
                    events.extend(self._from_messaging(connection, item, entry_time))

        changes = entry.get("changes")
        if isinstance(changes, list):
            for change in changes:
                if isinstance(change, dict) and change.get("field") == "feed":
                    events.extend(self._from_feed(connection, change.get("value"), entry_time))

        if isinstance(entry.get("standby"), list):
            # The handover protocol: another app owns the thread right now. Out of
            # scope for v1, and acting on it would mean answering a conversation
            # somebody else is holding.
            logger.debug("Ignoring a standby batch on connection %s.", connection.pk)
        return events

    def _from_messaging(
        self,
        connection: ChannelConnection,
        item: dict[str, Any],
        entry_time: Any,
    ) -> list[NormalizedEvent]:
        """One ``messaging`` item. Usually one event; a postback with a referral is two."""
        sender = item.get("sender")
        psid = meta_common.bounded_id(sender.get("id") if isinstance(sender, dict) else None)
        if not psid:
            return []
        timestamp = _timestamp(item.get("timestamp"), entry_time)
        # What the platform actually sent, for the two events whose id is derived
        # rather than given. See :func:`_event_clock`.
        clock = _event_clock(item.get("timestamp"), entry_time)

        message = item.get("message")
        if isinstance(message, dict):
            return self._from_message(connection, item, message, psid, timestamp)

        postback = item.get("postback")
        if isinstance(postback, dict):
            return self._from_postback(connection, item, postback, psid, timestamp, clock)

        referral = item.get("referral")
        if isinstance(referral, dict):
            return self._referral_event(connection, item, referral, psid, timestamp, clock, source="ref")

        delivery = item.get("delivery")
        if isinstance(delivery, dict):
            return self._delivery_events(connection, item, delivery, psid, timestamp)

        read = item.get("read")
        if isinstance(read, dict):
            return self._read_events(connection, item, read, psid, timestamp)

        # optin, account_linking, reaction, message_edit and the rest: real Meta
        # payloads this adapter does not carry. Dropped rather than half-parsed.
        return []

    def _from_message(
        self,
        connection: ChannelConnection,
        item: dict[str, Any],
        message: dict[str, Any],
        psid: str,
        timestamp: datetime,
    ) -> list[NormalizedEvent]:
        if message.get("is_echo"):
            # Our own send, mirrored back because the page is subscribed to
            # ``messages``. Ingesting it would file every outbound message as an
            # inbound one and reopen the messaging window on our own traffic.
            return []

        mid = meta_common.bounded_id(message.get("mid"))
        if not mid:
            # Every genuine message has one, and it is the deduplication key
            # (SPEC §7.1 step 2). Without it a redelivery would be processed again.
            logger.info("A Messenger message on connection %s carried no usable mid.", connection.pk)
            return []

        text = _text(message.get("text"))
        attachments = _attachment_urls(message)
        extra = _sender_extra(item)

        quick_reply = message.get("quick_reply")
        if isinstance(quick_reply, dict):
            payload = _text(quick_reply.get("payload"), MAX_PAYLOAD_BYTES)
            if payload:
                # A tapped chip is a button press, not prose — the same thing
                # Telegram delivers as a callback query and this project calls a
                # postback. Emitting it as one keeps a tap from firing a keyword
                # trigger on the chip's own label, and keeps SPEC §9.3's default
                # reply from answering "I didn't understand that" to a button.
                return [
                    NormalizedEvent(
                        type=EventType.POSTBACK,
                        connection=connection,
                        platform_user_id=psid,
                        provider_event_id=f"fb:{mid}",
                        timestamp=timestamp,
                        payload=EventPayload(
                            text=text,
                            button_id=_button_id(payload),
                            extra={**extra, "quick_reply_payload": payload[:MAX_EXTRA_CHARS]},
                        ),
                        raw=item,
                    )
                ]

        if not text and not attachments:
            # A sticker with no fallback, a reaction, an unsupported type. Nothing
            # a thread row could usefully hold.
            return []

        return [
            NormalizedEvent(
                type=EventType.MESSAGE,
                connection=connection,
                platform_user_id=psid,
                provider_event_id=f"fb:{mid}",
                timestamp=timestamp,
                payload=EventPayload(text=text, attachments=attachments, extra=extra),
                raw=item,
            )
        ]

    def _from_postback(
        self,
        connection: ChannelConnection,
        item: dict[str, Any],
        postback: dict[str, Any],
        psid: str,
        timestamp: datetime,
        clock: str,
    ) -> list[NormalizedEvent]:
        """A button press, and the referral that may be riding with it.

        SPEC §6.4 and issue #18: an m.me ref arrives *either* standalone *or*
        inside the get-started postback of somebody opening the conversation for
        the first time. Both are the same trigger, so the second case emits two
        events — a postback for the press and a referral for the ref — rather than
        picking one and losing the other.
        """
        payload = _text(postback.get("payload"), MAX_PAYLOAD_BYTES)
        title = _text(postback.get("title"), MAX_EXTRA_CHARS)
        extra = _sender_extra(item)

        events: list[NormalizedEvent] = []
        if payload:
            events.append(
                NormalizedEvent(
                    type=EventType.POSTBACK,
                    connection=connection,
                    platform_user_id=psid,
                    # Meta sends no id for a postback, so it is derived from the
                    # content — with the timestamp in it, which is what keeps two
                    # genuine presses of the same button from colliding.
                    provider_event_id=channels_ingest.synthetic_event_id(
                        {"psid": psid, "payload": payload, "ts": clock},
                        prefix="fb:pb:",
                    ),
                    timestamp=timestamp,
                    payload=EventPayload(
                        button_id=_button_id(payload),
                        extra={**extra, "postback_payload": payload[:MAX_EXTRA_CHARS], "title": title},
                    ),
                    raw=item,
                )
            )

        referral = postback.get("referral")
        if isinstance(referral, dict) and _referral_ref(referral):
            events.extend(self._referral_event(connection, item, referral, psid, timestamp, clock, source="postback"))
        return events

    def _referral_event(
        self,
        connection: ChannelConnection,
        item: dict[str, Any],
        referral: dict[str, Any],
        psid: str,
        timestamp: datetime,
        clock: str,
        *,
        source: str,
    ) -> list[NormalizedEvent]:
        """SPEC §10's ref_url trigger: ``m.me/<page>?ref=<ref>``.

        ``apps.flows.triggers.stages.REPLY_EVENTS`` deliberately keeps referrals
        away from ``attempt_resume`` — a ref handed to a waiting execution would be
        swallowed by a retry prompt — so this is a trigger-stage event and nothing
        else. A referral with no ref is Messenger's "arrived with no payload", which
        ``matching._is_welcome`` reads as the welcome signal.
        """
        ref = _referral_ref(referral)
        return [
            NormalizedEvent(
                type=EventType.REFERRAL,
                connection=connection,
                platform_user_id=psid,
                provider_event_id=channels_ingest.synthetic_event_id(
                    {"psid": psid, "ref": ref, "ts": clock, "src": source},
                    prefix="fb:ref:",
                ),
                timestamp=timestamp,
                payload=EventPayload(ref=ref, extra={**_sender_extra(item), **_referral_extra(referral)}),
                raw=item,
            )
        ]

    def _delivery_events(
        self,
        connection: ChannelConnection,
        item: dict[str, Any],
        delivery: dict[str, Any],
        psid: str,
        timestamp: datetime,
    ) -> list[NormalizedEvent]:
        """``message_deliveries``: one receipt per message id Meta names.

        The payload convention is ``apps.messaging.ingest``'s, written down in that
        module because it is the module that reads it:
        ``extra = {"provider_message_id": ..., "status": ...}``. The adapter never
        touches ``message.status`` itself — ROADMAP contract 1 gives that to the
        messaging facade, and ``apps/messaging/tests/test_write_sites.py`` scans
        the AST for a second writer.
        """
        mids = delivery.get("mids")
        if not isinstance(mids, list):
            return []
        if len(mids) > MAX_DELIVERY_MIDS:
            # Said out loud rather than sliced silently: everything dropped here
            # is a message that will sit at ``sent`` forever, because Meta does
            # not resend a delivery receipt.
            logger.warning(
                "Messenger: a delivery receipt named %s message ids; only the first %s were applied.",
                len(mids),
                MAX_DELIVERY_MIDS,
            )
        events: list[NormalizedEvent] = []
        for raw in mids[:MAX_DELIVERY_MIDS]:
            mid = meta_common.bounded_id(raw)
            if not mid:
                continue
            events.append(
                self._receipt(connection, item, psid, timestamp, mid=mid, status="delivered", event_id=f"fb:dlv:{mid}")
            )
        return events

    def _read_events(
        self,
        connection: ChannelConnection,
        item: dict[str, Any],
        read: dict[str, Any],
        psid: str,
        timestamp: datetime,
    ) -> list[NormalizedEvent]:
        """``message_reads``: a **watermark**, which has to be resolved to messages.

        Meta does not say *which* messages were read — it says "everything this
        person was sent up to this moment was". ``apps.messaging.ingest`` maps a
        receipt onto a row by ``provider_message_id``, so the watermark has to be
        resolved here into ids before it means anything.

        Four properties keep that honest.

        *It is scoped to this person.* The receipt came from one PSID, so the
        query is narrowed to that contact's thread and not merely to the
        connection — a page talks to thousands of people at once, and a
        connection-wide query would mark one person's unread messages read
        because somebody else opened Messenger.

        *It is read-only and workspace-scoped.* The status itself still moves
        only through ``apps.messaging.ingest``; contract 1 and the AST scan in
        ``apps/messaging/tests/test_write_sites.py`` see to that.

        *It is bounded* to the most recent ``MAX_READ_RECEIPT_MESSAGES``, so a
        forged watermark cannot turn into an unbounded scan.

        *It narrows to messages not already read*, so a repeated watermark costs
        one indexed query and produces nothing.

        The watermark is compared against our own ``created_at``, which is the
        only join the two clocks allow. A receipt arrives after the message it
        refers to, by however long the person took to look, so ordinary clock
        skew is far smaller than the gap — and a watermark far enough in the past
        to miss its own message resolves to nothing rather than to the wrong
        thing, which is the direction to fail in.

        The import is deferred because ``apps.messaging`` imports ``apps.channels``
        — a module-scope import here would be a cycle.
        """
        raw_watermark = read.get("watermark")
        cutoff = _epoch_ms(raw_watermark)
        if cutoff is None:
            # **Fails closed, and that is the whole point.** ``_timestamp`` would
            # have answered ``now()`` here, which is the most permissive cutoff
            # there is: a watermark of ``Infinity`` — which ``json.loads`` accepts
            # by default — would mark this contact's entire recent thread read,
            # including messages they demonstrably have not seen. A receipt we
            # cannot read refers to nothing.
            logger.info("Messenger: a read receipt on connection %s carried no usable watermark.", connection.pk)
            return []
        if self._read_receipt_budget <= 0:
            logger.info(
                "Messenger: more read receipts in one delivery than %s; the rest were dropped.",
                MAX_READ_RECEIPTS_PER_DELIVERY,
            )
            return []
        self._read_receipt_budget -= 1

        from apps.messaging.models import ContactChannelIdentity, Message, MessageDirection, MessageStatus

        contact_id = (
            ContactChannelIdentity.objects.for_workspace(connection.workspace_id)
            .filter(channel_connection=connection, platform_user_id=psid)
            .values_list("contact_id", flat=True)
            .first()
        )
        if contact_id is None:
            return []

        mids = list(
            Message.objects.for_workspace(connection.workspace_id)
            .filter(
                channel_connection=connection,
                conversation__contact_id=contact_id,
                direction=MessageDirection.OUT,
                status__in=(MessageStatus.SENT, MessageStatus.DELIVERED),
                # ``updated_at``, not ``created_at``: a row is created when the
                # message is *queued* and stamped again when the send is
                # finalised. A message queued at T and actually sent an hour later
                # — rate-limit deferral, or a retry after an outage — would
                # otherwise match a watermark from the intervening half hour and
                # be marked read before it had reached the platform at all.
                updated_at__lte=cutoff,
            )
            .exclude(provider_message_id="")
            .order_by("-created_at")
            .values_list("provider_message_id", flat=True)[:MAX_READ_RECEIPT_MESSAGES]
        )
        return [
            self._receipt(
                connection,
                item,
                psid,
                timestamp,
                mid=mid,
                status="read",
                # The watermark is part of the id so a later read of a later
                # message is a new event rather than a duplicate of this one.
                # Formatted from the *parsed* value, never from the raw one:
                # ``int(float("inf"))`` raises OverflowError and ``int(float("nan"))``
                # raises ValueError, and either would escape a parser whose whole
                # contract is that it never raises — costing the entire delivery,
                # including every good event batched beside this receipt.
                event_id=f"fb:read:{cutoff.isoformat()}:{mid}",
            )
            for mid in mids
        ]

    def _receipt(
        self,
        connection: ChannelConnection,
        item: dict[str, Any],
        psid: str,
        timestamp: datetime,
        *,
        mid: str,
        status: str,
        event_id: str,
    ) -> NormalizedEvent:
        return NormalizedEvent(
            type=EventType.DELIVERY_STATUS,
            connection=connection,
            platform_user_id=psid,
            provider_event_id=event_id,
            timestamp=timestamp,
            payload=EventPayload(extra={"provider_message_id": mid, "status": status}),
            raw=item,
        )

    def _from_feed(
        self,
        connection: ChannelConnection,
        value: Any,
        entry_time: Any,
    ) -> list[NormalizedEvent]:
        """A ``feed`` change, when it is somebody commenting on one of our posts.

        SPEC §10's comment trigger. The event carries the comment's id in
        ``payload.comment_id`` and its post and parent in ``payload.extra`` under
        the keys ``apps.flows.triggers.types`` fixes — L4-A's matcher and its
        once-only guard read exactly those, so this adapter adds a parser and
        nothing else.

        Two filters that matter more than the parsing:

        *Only ``verb: add``.* An edited or deleted comment is not a new person
        asking for something, and answering one would send a private reply to
        somebody we already answered.

        *Never our own comments.* A page's public reply arrives back through the
        same ``feed`` subscription, so without this the reply we just posted would
        match the trigger and start a flow at ourselves.
        """
        if not isinstance(value, dict) or value.get("item") != "comment" or value.get("verb") != "add":
            return []

        comment_id = meta_common.bounded_id(value.get("comment_id"))
        if not comment_id:
            return []

        author = value.get("from")
        author_id = meta_common.bounded_id(author.get("id") if isinstance(author, dict) else None)
        if not author_id or author_id == connection.external_id:
            return []

        post_id = meta_common.bounded_id(value.get("post_id"))
        parent_id = meta_common.bounded_id(value.get("parent_id"))
        if parent_id == post_id:
            # Meta sets ``parent_id`` to the *post* on a top-level comment and to
            # the parent comment on a reply. ``apps.flows.triggers.types`` reads an
            # empty parent as "top level", so the two have to be told apart here or
            # SPEC §10's ``top_level_only`` would match nothing.
            parent_id = ""

        text = _text(value.get("message"))
        extra: dict[str, Any] = {COMMENT_POST_ID_KEY: post_id, COMMENT_TEXT_KEY: text[:MAX_EXTRA_CHARS]}
        if parent_id:
            extra[COMMENT_PARENT_ID_KEY] = parent_id
        author_name = _text(author.get("name"), MAX_EXTRA_CHARS) if isinstance(author, dict) else ""
        if author_name:
            extra["commenter_name"] = author_name

        return [
            NormalizedEvent(
                type=EventType.COMMENT,
                connection=connection,
                platform_user_id=author_id,
                provider_event_id=f"fb:comment:{comment_id}",
                timestamp=_timestamp(_seconds_to_ms(value.get("created_time")), entry_time),
                payload=EventPayload(text=text, comment_id=comment_id, extra=extra),
                raw={"field": "feed", "value": value},
            )
        ]

    # -- outbound -----------------------------------------------------------

    def send(self, connection: ChannelConnection, identity: Any, outbound: OutboundMessage) -> SendResult:
        """Deliver one message, downgrading it first (SPEC §6.1).

        The downgrade can turn one abstract message into several — a caption
        becomes its own bubble, a long gallery becomes several templates — and they
        go in order. The result reports the **last** provider id: it is the message
        the contact is looking at, and the one a delivery receipt will reference.

        **A multi-part send is not atomic, and cannot be here.** If the third of
        three calls fails, the first two have arrived, and the retry (SPEC §9.4
        keys idempotency on the *message row*, of which there is one) sends all
        three again. The behaviour is: duplicate rather than drop, which is the
        right direction for a message a flow author intended to send, and it is
        written down here rather than discovered in production —
        ``telegram.send`` says the same about the same trade.
        """
        psid = str(getattr(identity, "platform_user_id", "") or "")
        if not psid:
            return SendResult(status=SendStatus.FAILED, error="no_recipient")

        tag = outbound.tag or None
        if tag and tag not in ALLOWED_TAGS:
            # Unreachable through the compliance engine, which only ever sets a
            # tag from this platform's own policy row. Refused rather than dropped
            # because Meta restricts pages over a message sent under a tag it did
            # not accept, and a send that silently lost its tag would be exactly
            # that message.
            logger.error("Messenger: refusing a send tagged %r, which this platform does not accept.", tag)
            return SendResult(status=SendStatus.FAILED, error="unsupported_tag")

        rendered = downgrade(outbound, self.capabilities)
        claim = self._pending_private_reply(connection, psid)
        recipient: dict[str, Any] = {"comment_id": claim.comment_id} if claim is not None else {"id": psid}

        calls: list[dict[str, Any]] = []
        for message in rendered.messages:
            calls.extend(wire_calls(recipient, message, tag=tag))
        if not calls:
            # Nothing sendable survived. Reported rather than silently counted as
            # sent, so contract 1's message row says what happened.
            return SendResult(status=SendStatus.FAILED, error="empty_message")

        provider_message_id = ""
        for index, body in enumerate(calls):
            if index and claim is not None:
                # Meta's private reply spends exactly one message. Everything
                # after it goes to the person, through the window the comment
                # opened.
                body = {**body, "recipient": {"id": psid}}
            try:
                result = send_body(connection, body)
            except APIError as exc:
                self._handle_send_error(connection, psid, exc)
                raise
            if index == 0 and claim is not None:
                self._settle_private_reply(claim, identity, result, psid)
            provider_message_id = meta_common.bounded_id(result.get("message_id")) or provider_message_id
        return SendResult(status=SendStatus.SENT, provider_message_id=provider_message_id)

    def _pending_private_reply(self, connection: ChannelConnection, psid: str) -> Any:
        """The comment this send is still owed as a private reply, or None.

        SPEC §10 makes the private reply "the flow's first message", so the
        question is asked here rather than answered by a separate opener message:
        Meta allows exactly one message in reply to a comment, and an opener
        followed by the flow's real first message would have the second one
        refused.

        ``apps.flows.triggers.comments`` answers it, keyed on the page-scoped id
        rather than on the contact — the claim predates the contact by design.
        The import is deferred and the failure is swallowed: a comment bookkeeping
        problem must not stop an ordinary DM going out.
        """
        try:
            from apps.flows.triggers import comments

            return comments.pending_private_reply(connection, psid)
        except Exception:
            logger.exception("Messenger: could not check for a pending private reply on connection %s.", connection.pk)
            return None

    def _settle_private_reply(self, claim: Any, identity: Any, result: dict[str, Any], psid: str) -> None:
        """Record that SPEC §10's one private reply has now been spent."""
        answered = meta_common.bounded_id(result.get("recipient_id"))
        if answered and answered != psid:
            # Meta answers a private reply with the page-scoped id it resolved the
            # commenter to. It should equal the id the comment carried; if it ever
            # does not, the identity we created is addressing somebody else and an
            # operator needs to know rather than find out through a misdelivered
            # conversation.
            logger.warning(
                "Messenger: a private reply resolved to a different page-scoped id than the comment carried."
            )
        try:
            from apps.flows.triggers import comments

            comments.mark_private_reply_sent(claim, contact=getattr(identity, "contact", None))
        except Exception:
            logger.exception("Messenger: could not record that a private reply was sent.")

    def _handle_send_error(self, connection: ChannelConnection, psid: str, exc: APIError) -> None:
        """Park a dead credential, or record an opt-out, then let the error carry on.

        Two failures mean something durable rather than "try later".

        A rejected token (Meta's ``190``, or a 401) means the page has to be
        reconnected, and every further send is a wasted call — so the connection is
        parked and the workspace's admins are told, through the
        ``channel_needs_reauth`` notification SPEC §5 already reserved.

        A ``551`` means the person blocked the page, deleted their account, or the
        thread is gone. All three mean never send here again, and continuing to try
        is what gets a page restricted. The adapter does **not** write
        ``identity.opted_out_at`` itself — ROADMAP contract 3 reserves that field
        for the ingest pipeline — so it raises the event the pipeline already knows
        how to apply, exactly as ``telegram._handle_send_error`` does.
        """
        if meta_common.is_reauth_error(exc):
            meta_common.mark_needs_reauth(connection, platform_label="Facebook Messenger")
            return
        if exc.code not in USER_UNAVAILABLE_CODES:
            return

        logger.info("Messenger: connection %s can no longer reach a person; recording an opt-out.", connection.pk)
        now = timezone.now()
        event = NormalizedEvent(
            type=EventType.OPT_OUT,
            connection=connection,
            platform_user_id=psid,
            # Timestamped rather than content-only: somebody who blocks, unblocks
            # and blocks again is two events, not one duplicate.
            provider_event_id=f"fb:blocked:{psid}:{int(now.timestamp())}",
            timestamp=now,
            payload=EventPayload(extra={"reason": "unavailable"}),
        )
        try:
            channels_ingest.process_events(connection, (event,))
        except Exception:
            # The send failure is the thing the caller is waiting to hear about.
            logger.exception("Messenger: could not record an opt-out on connection %s.", connection.pk)

    def send_typing(self, connection: ChannelConnection, identity: Any) -> None:
        """``sender_action: typing_on``. Cosmetic, so a failure is swallowed."""
        self._sender_action(connection, identity, "typing_on")

    def mark_seen(self, connection: ChannelConnection, identity: Any) -> None:
        """``sender_action: mark_seen``. Cosmetic, so a failure is swallowed."""
        self._sender_action(connection, identity, "mark_seen")

    def _sender_action(self, connection: ChannelConnection, identity: Any, action: str) -> None:
        psid = str(getattr(identity, "platform_user_id", "") or "")
        if not psid:
            return
        try:
            send_body(connection, {"recipient": {"id": psid}, "sender_action": action})
        except APIError:
            logger.debug("Messenger: %s failed on connection %s.", action, connection.pk)

    # -- lifecycle ----------------------------------------------------------

    def on_webhook_secret_rotated(self, connection: ChannelConnection, secret: str) -> None:
        """Nothing to push. Meta signs with the app secret, not a per-page one.

        Overridden rather than inherited so the reason is written where an
        operator's question lands: rotating a Messenger connection's webhook secret
        changes a value this platform never presents, and does **not** break
        delivery the way it would for Telegram. The app secret lives in
        Settings → Credentials and is rotated in Meta's console.
        """

    def on_disconnect(self, connection: ChannelConnection) -> None:
        """Unsubscribe the app, so a removed page stops delivering."""
        unsubscribe_page(connection)


def _seconds_to_ms(raw: Any) -> Any:
    """Meta's ``feed`` timestamps are seconds; the messaging ones are milliseconds."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return raw * 1000


# ---------------------------------------------------------------------------
# The comment-trigger follow-up (SPEC §10)
# ---------------------------------------------------------------------------


def enqueue_comment_actions(claim: Any) -> None:
    """Hand a freshly claimed comment to the worker, as two independent actions.

    Registered on ``apps.flows.triggers.comments``' seam, which is called from
    inside the webhook request. Answering a comment is a public reply, a like and
    a private reply — three round trips to Meta against SPEC §7.1's 1.5 s budget
    for the whole inline path — so this enqueues and returns, the way
    ``telegram._answer_callback_query`` does for a spinner.

    The two actions are keyed separately on the ``HandledComment`` row, so a
    redelivery or a retried routing pass produces no second copy of either.
    """
    row = claim.row
    connection = claim.connection
    payload = {"connection_id": str(connection.pk), "handled_comment_id": str(row.pk)}
    for action_type, key, attempts in (
        # **At most once.** Meta gives us no way to make a comment or a like
        # idempotent, so a retry means a second public reply under somebody's
        # comment — worse than the missing one a transient failure costs.
        (COMMENT_ACTION, f"fb-comment:{row.pk}", 1),
        # **Worth retrying.** Every step of it is idempotent, and it is the half
        # the public reply just promised the customer. Not for six hours, though:
        # the private reply has a seven-day deadline and a ten-minute hand-off
        # window, and the guards re-check both on every attempt.
        (COMMENT_DM_ACTION, f"fb-comment-dm:{row.pk}", 3),
    ):
        try:
            queue_schedule(
                action_type,
                timezone.now(),
                payload,
                workspace=connection.workspace,
                idempotency_key=key,
                max_attempts=attempts,
            )
        except Exception:
            logger.warning("Messenger: could not enqueue %s for %s.", action_type, row.pk)


def _claimed_comment(payload: dict[str, Any]) -> tuple[Any, Any] | None:
    """``(connection, row)`` for a queued comment action, or None to stop.

    Reads everything back by id rather than trusting the payload — the queue row
    is ours, but the ids in it were derived from an inbound webhook, and a handler
    that took a comment id or a page from a payload would be a way to make the
    worker post as an arbitrary page.
    """
    from apps.flows.models import HandledComment
    from apps.flows.triggers import guards

    connection_id = payload.get("connection_id")
    row_id = payload.get("handled_comment_id")
    if not isinstance(connection_id, str) or not isinstance(row_id, str):
        return None

    # Cross-tenant by necessity: a worker drains the whole deployment and has no
    # session workspace. The queue row it is acting on named this connection.
    connection = ChannelConnection.objects.unscoped().filter(pk=connection_id).first()
    if connection is None:
        return None
    row = (
        HandledComment.objects.for_workspace(connection.workspace_id)
        .filter(pk=row_id, channel_connection=connection)
        .select_related("trigger", "trigger__flow")
        .first()
    )
    if row is None or not guards.may_private_reply(row):
        # Already answered, or past SPEC §10's seven-day deadline. Both are
        # ordinary outcomes for a retry rather than failures.
        return None
    return connection, row


@register_handler(COMMENT_ACTION)
def _run_comment_actions(payload: dict[str, Any], action: Any) -> None:
    """The public half: reply to the comment and like it (SPEC §10).

    Scheduled at most once (see :data:`COMMENT_ACTION`), so nothing here has to be
    idempotent — and nothing here can be. Each step swallows its own failure, so a
    refused like does not cost the reply.
    """
    found = _claimed_comment(payload)
    if found is None:
        return
    connection, row = found
    config = row.trigger.config_json if row.trigger is not None else {}
    _public_reply(connection, row, config)
    _like_comment(connection, row, config)


@register_handler(COMMENT_DM_ACTION)
def _run_comment_dm(payload: dict[str, Any], action: Any) -> None:
    """The private half: open the DM thread and start the trigger's flow.

    Retryable, and it **raises** rather than logging when something transient goes
    wrong — that is the difference the split buys. Before it, a failure here put
    the whole handler back on the queue and the public reply was posted again.
    """
    found = _claimed_comment(payload)
    if found is None:
        return
    connection, row = found
    _start_comment_flow(connection, row)


def _public_reply(connection: ChannelConnection, row: Any, config: dict[str, Any]) -> None:
    """SPEC §10's ``public_reply``: none, one fixed text, or one picked at random."""
    reply = config.get("public_reply")
    if not isinstance(reply, dict):
        return
    mode = reply.get("mode")
    raw = reply.get("texts")
    # A list, checked rather than assumed: ``config_json`` is a JSONField, and
    # iterating a bare string here would post a single character.
    texts = [text for text in raw if isinstance(text, str) and text.strip()] if isinstance(raw, list) else []
    if mode == "static":
        message = texts[0] if texts else ""
    elif mode == "random":
        message = secrets.choice(texts) if texts else ""
    else:
        return
    if not message:
        return
    try:
        meta_common.graph_call(
            meta_common.page_token(connection),
            "POST",
            f"{row.comment_id}/comments",
            json={"message": message},
            timeout=BACKGROUND_TIMEOUT,
        )
    except APIError:
        logger.info("Messenger: the public reply to a comment on connection %s was refused.", connection.pk)


def _like_comment(connection: ChannelConnection, row: Any, config: dict[str, Any]) -> None:
    """SPEC §10's ``like_comment``. Cosmetic, so a failure is logged and swallowed."""
    if not config.get("like_comment"):
        return
    try:
        meta_common.graph_call(
            meta_common.page_token(connection),
            "POST",
            f"{row.comment_id}/likes",
            timeout=BACKGROUND_TIMEOUT,
        )
    except APIError:
        logger.debug("Messenger: liking a comment failed on connection %s.", connection.pk)


def _start_comment_flow(connection: ChannelConnection, row: Any) -> None:
    """Open the DM thread and start the trigger's flow (SPEC §10).

    The interesting half, and the one thing about this path that is not obvious.

    A comment creates **no contact** — ``apps.messaging.ingest`` says so at length,
    because one viral post would otherwise be a contact-spam amplifier — so before
    a flow can run for this person there has to be an identity, and before a
    message can go out there has to be an open messaging window. Both are written
    by exactly one place in the project (contract 3, enforced by an AST scan), and
    that place is ``apps.messaging.ingest.persist_events`` applying an inbound
    event.

    So the transition is expressed as the event it actually is: the commenter has
    started a conversation. ``persist_events`` treats a ``referral`` as
    contact-authored activity — identity, consent and a 24-hour window, and no
    message row, per that module's own table — which is exactly right here, and
    matches Meta's rule that a private reply opens the standard window.

    ``persist_events`` rather than ``process_events``, deliberately: this event
    must not go round the routing stages, or the comment's own text could fire a
    keyword trigger on top of the comment trigger that already matched it.
    """
    from apps.flows.triggers import comments
    from apps.messaging import ingest as messaging_ingest
    from apps.messaging.models import ContactChannelIdentity

    if row.trigger is None:
        logger.info("Messenger: a claimed comment has no trigger left to run.")
        return
    if not row.commenter_ref:
        # Nothing to address. Meta omits ``from`` on a comment when the app lacks
        # the engagement permissions, and a fabricated identity would be worse
        # than no private reply at all.
        logger.info("Messenger: a claimed comment carries no commenter id; not opening a thread.")
        return

    messaging_ingest.persist_events(
        connection,
        [
            NormalizedEvent(
                type=EventType.REFERRAL,
                connection=connection,
                platform_user_id=row.commenter_ref,
                provider_event_id=f"fb:comment-dm:{row.comment_id}",
                timestamp=timezone.now(),
                payload=EventPayload(extra={"source": "comment", "comment_id": row.comment_id}),
            )
        ],
    )

    identity = (
        ContactChannelIdentity.objects.for_workspace(connection.workspace_id)
        .filter(channel_connection=connection, platform_user_id=row.commenter_ref)
        .select_related("contact")
        .first()
    )
    if identity is None or identity.contact is None:
        # ``persist_events`` catches and logs its own per-event failures, so the
        # only way to learn it did not write the identity is to look. **Raised**,
        # not logged: this is transient (a contact-row race, a database blip), the
        # customer has just been promised a DM in public, and this action is the
        # retryable half precisely so that promise is kept. Returning quietly here
        # marked the action done and lost the conversation for good.
        raise RuntimeError(f"opening a thread from comment {row.pk} produced no contact")

    try:
        comments.start_claimed_flow(row, identity.contact, connection)
    except comments.ClaimedFlowNotRunnableError as exc:
        # A trigger pointing at a flow with no publishable version. A configuration
        # problem retries cannot fix, so it is logged and the claim stands.
        logger.warning("Messenger: comment trigger %s cannot start its flow: %s", row.trigger_id, exc)


# ---------------------------------------------------------------------------
# The comment-trigger post picker
# ---------------------------------------------------------------------------


def list_posts(connection: ChannelConnection, limit: int) -> list[Any]:
    """Recent posts on this page, for the comment trigger's post picker.

    Registered on ``apps.channels.posts``' seam. The fields are the smallest set
    that lets a person recognise a post: its id (which is what a trigger stores),
    its own text, and a link to look at it.
    """
    try:
        body = meta_common.graph_call(
            meta_common.page_token(connection),
            "GET",
            f"{connection.external_id}/posts",
            params={"fields": "id,message,permalink_url,created_time", "limit": limit},
            timeout=BACKGROUND_TIMEOUT,
        )
    except APIError as exc:
        # The message names the host and nothing else (``request_json``), so it is
        # safe to log — but it is not shown to the operator, who gets the picker's
        # own wording instead.
        logger.info("Messenger: listing posts failed on connection %s (%s).", connection.pk, exc.code or "no code")
        raise PostListingError("The page's posts could not be listed.") from exc

    rows = body.get("data")
    if not isinstance(rows, list):
        return []
    posts = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        post = clean_post(
            post_id=item.get("id"),
            title=item.get("message"),
            permalink=item.get("permalink_url"),
            created_time=item.get("created_time"),
        )
        if post is not None:
            posts.append(post)
    return posts


register_adapter(Platform.MESSENGER, MessengerAdapter)
register_post_lister(Platform.MESSENGER, list_posts)


def _register_comment_actions() -> None:
    """Claim ``apps.flows.triggers.comments``' Messenger slot.

    Through a function with a local import rather than a module-scope one:
    ``apps.channels`` is readied before ``apps.flows`` and has no module-level
    dependency on it, and this is not the place to introduce one. The import is
    legal here because ``AppConfig.ready`` runs after Django has populated every
    app's models.
    """
    from apps.flows.triggers.comments import register_comment_actions

    register_comment_actions(Platform.MESSENGER, enqueue_comment_actions, replace=True)


_register_comment_actions()
