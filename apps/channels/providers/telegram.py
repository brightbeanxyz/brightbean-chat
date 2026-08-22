"""Telegram Bot API adapter (SPEC §6.2) — the first real adapter in the repo.

ROADMAP contract 4 promises that a platform costs "one module in
``channels/providers/`` and one registry entry". This is the module that has to
make that promise true, so the parts that are *about Telegram* are kept visibly
separate from the parts that are *about being an adapter*:

* the HTTP mechanics, the timeout policy, the 429 → :class:`RateLimitError`
  mapping and the "never put a URL in an error message" rule are inherited from
  :mod:`apps.channels.providers.base` and are not re-implemented here;
* block downgrading is :func:`apps.channels.downgrade.downgrade`, shared;
* what is genuinely Telegram's — the update shapes, the method names, the
  keyboard json, the 1024-character caption cap — lives in the small helpers
  below, each named for the thing it converts.

A Layer-5 author copying this should be able to replace the helpers and keep
the class.

Why Telegram is first: it is the cheapest complete loop. One BotFather token,
one ``setWebhook`` call, no OAuth, no app review.

--------------------------------------------------------------------------
Rate limits, and why there is no throttle in this file
--------------------------------------------------------------------------

SPEC §6.2 gives two numbers: roughly 1 message per second per chat, and roughly
30 per second overall. Neither needs code here.

The **global** limit is the connection's token bucket, already implemented in
``apps.messaging.buckets`` and configured by ``rate_default=25.0`` in
:mod:`apps.channels.policy` — a little under Telegram's ceiling on purpose.

The **per-chat** limit is satisfied by the shape of the system rather than by a
timer: SPEC §9.6 serialises everything a contact does behind one advisory lock
and SPEC §9.2 allows one live execution per contact, so two messages to the same
chat cannot be in flight at once. A second throttle here would be a sleep held
inside that lock — it would not make sends safer, it would make the lock longer.
When Telegram disagrees anyway it says so with a 429 and a ``retry_after``, and
the send pipeline reschedules (``apps.messaging.services._defer``).

--------------------------------------------------------------------------
Secrets
--------------------------------------------------------------------------

The bot token *is* the bot: anyone holding it can read every message and send as
the bot. It lives encrypted in ``connection.credentials`` and appears in exactly
one place at runtime — the path of the Bot API URL. Nothing in this module logs
a URL. :func:`apps.channels.providers.base.request_json` reports the *host* of a
failed call and never the path, and ``apps.common.logging`` scrubs the
``<bot_id>:<secret>`` shape from anything that gets through anyway
(SECURITY-BASELINE §5).
"""

import logging
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
from apps.channels.providers.base import BACKGROUND_TIMEOUT, Adapter, request_json
from apps.channels.providers.exceptions import APIError
from apps.channels.registry import register_adapter
from apps.common.platforms import Platform

if TYPE_CHECKING:
    from django.http import HttpRequest

logger = logging.getLogger(__name__)

__all__ = [
    "SECRET_HEADER",
    "TelegramAdapter",
    "bot_token",
    "call",
    "deep_link",
    "delete_webhook",
    "get_me",
    "set_webhook",
    "wire_calls",
]

#: The Bot API root. A constant rather than a setting: there is one Bot API, and
#: a configurable host on a path that carries the token is an exfiltration
#: primitive rather than a feature.
API_ROOT = "https://api.telegram.org"

#: The header Telegram echoes the ``secret_token`` given to ``setWebhook`` in.
SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"  # noqa: S105 - a header name, not a credential

#: Where the token sits inside ``connection.credentials``.
TOKEN_KEY = "bot_token"  # noqa: S105 - a dict key, not a credential

#: Update types we ask Telegram to deliver. An allowlist rather than the default
#: (everything but ``chat_member``), because every type we do not handle is a
#: delivery that costs a request, a dedup row and a parse to discard. Groups,
#: channels, inline mode and payments are all out of scope for v1 (issue #12).
ALLOWED_UPDATES: tuple[str, ...] = ("message", "callback_query")

#: ``sendMessage`` caps text at 4096 characters; the downgrade renderer has
#: already applied it from :mod:`apps.channels.capabilities`. Read from the same
#: table rather than restated, so the two cannot drift.
_CAPABILITIES: Capabilities = capabilities_for(Platform.TELEGRAM)
MAX_TEXT_CHARS = _CAPABILITIES.max_text_len

#: Media captions are capped separately, and lower. There is no field for this
#: on :class:`~apps.channels.capabilities.Capabilities` — it is not something the
#: flow builder warns about — so it is an adapter constant.
MAX_CAPTION_CHARS = 1024

#: ``callback_data`` is capped at 64 **bytes**, not characters.
MAX_CALLBACK_DATA_BYTES = 64

#: Longest inbound text we carry out of a parse. Telegram's own cap is 4096;
#: ``apps.messaging.ingest`` bounds it again downstream, and this one exists so
#: a hostile payload cannot make us hold an arbitrarily long string in the first
#: place (SECURITY-BASELINE §§2, 7).
MAX_INBOUND_TEXT_CHARS = MAX_TEXT_CHARS

#: A ``t.me/<bot>?start=<payload>`` payload is capped at 64 characters by
#: Telegram, so a ref longer than that never came from a real deep link.
MAX_REF_CHARS = 64

#: Longest attacker-supplied display string we keep in ``payload.extra``.
MAX_EXTRA_CHARS = 200

#: The ``/start`` command, which SPEC §10 maps to two different triggers
#: depending on whether it carries a payload.
START_COMMAND = "/start"

#: ``message`` keys that carry media, and the block kind each becomes. The
#: aliases are folded in deliberately: a voice note is audio and a video note is
#: video as far as anything downstream is concerned, and giving each its own
#: kind would mean every consumer learning Telegram's vocabulary.
_MEDIA_FIELDS: tuple[tuple[str, str], ...] = (
    ("photo", "image"),
    ("audio", "audio"),
    ("voice", "audio"),
    ("video", "video"),
    ("video_note", "video"),
    ("animation", "video"),
    ("document", "file"),
    ("sticker", "image"),
)

#: Block kind -> (Bot API method, the payload key the media goes in).
_MEDIA_METHODS: dict[str, tuple[str, str]] = {
    "image": ("sendPhoto", "photo"),
    "audio": ("sendAudio", "audio"),
    "video": ("sendVideo", "video"),
    "file": ("sendDocument", "document"),
}


# ---------------------------------------------------------------------------
# The Bot API client
# ---------------------------------------------------------------------------


def _client() -> httpx.Client | None:
    """The HTTP client every Bot API call goes through.

    None means "one client per call", which is what
    :func:`~apps.channels.providers.base.request_json` does by default.

    **This is the test seam**, mirroring ``request_json``'s own ``client=``
    parameter: a test monkeypatches this function to return an
    ``httpx.Client(transport=httpx.MockTransport(...))`` and the whole module —
    including the real error mapping, the real 429 handling and the real
    payload building — runs without a socket.
    """
    return None


def bot_token(connection: ChannelConnection) -> str:
    """The bot token stored on ``connection``, or "" when there is none.

    ``credentials`` is an encrypted column, so reading it can fail on a
    deployment whose key has changed. That is a configuration problem and not
    something a webhook or a send should turn into a 500, so it reads as "no
    token" and the caller fails the operation with a named error.
    """
    try:
        credentials: Any = connection.credentials or {}
    except ValueError:
        logger.error("Connection %s: credentials could not be decrypted.", connection.pk)
        return ""
    if not isinstance(credentials, dict):
        return ""
    token = credentials.get(TOKEN_KEY)
    return token if isinstance(token, str) else ""


def call(
    token: str,
    method: str,
    payload: dict[str, Any] | None = None,
    *,
    timeout: float | None = None,
) -> Any:
    """One Bot API method call. Returns the ``result`` member of the response.

    Raises :class:`~apps.channels.providers.exceptions.APIError` — or
    :class:`~apps.channels.providers.exceptions.RateLimitError` on a 429, with
    ``retry_after`` filled in from Telegram's ``parameters.retry_after`` — via
    ``request_json``. The only case ``request_json`` cannot see is a 200 whose
    body says ``ok: false``, which the Bot API is not supposed to produce; it is
    turned into an ``APIError`` here rather than returned as a result nobody
    checked.

    ``timeout`` defaults to the inline budget SPEC §7.1 sets. Connect-time calls
    pass ``BACKGROUND_TIMEOUT``: nobody is waiting on a webhook for those.
    """
    if not token:
        raise APIError("This Telegram connection has no bot token stored.")
    body = request_json(
        "POST",
        f"{API_ROOT}/bot{token}/{method}",
        json=payload or {},
        client=_client(),
        timeout=timeout,
    )
    if not body.get("ok"):
        # No `description` in the message: Telegram's prose quotes the request
        # that caused it, and these strings reach logs and the inbox
        # (SECURITY-BASELINE §5). The numeric code is what a human looks up.
        raise APIError(f"Telegram refused {method}", code=str(body.get("error_code") or "")[:64])
    return body.get("result")


def get_me(token: str) -> dict[str, Any]:
    """``getMe``: proves the token works and says which bot it belongs to."""
    result = call(token, "getMe", timeout=BACKGROUND_TIMEOUT)
    if not isinstance(result, dict):
        raise APIError("Telegram returned an unexpected getMe result")
    return result


def set_webhook(token: str, *, url: str, secret_token: str) -> None:
    """Point the bot at ``url`` and pin the secret it must present.

    ``drop_pending_updates`` is on: a bot connected today should not replay a
    backlog of messages sent to it before this workspace existed, which would
    arrive as inbound events and could fire triggers.
    """
    call(
        token,
        "setWebhook",
        {
            "url": url,
            "secret_token": secret_token,
            "allowed_updates": list(ALLOWED_UPDATES),
            "drop_pending_updates": True,
            "max_connections": 40,
        },
        timeout=BACKGROUND_TIMEOUT,
    )


def delete_webhook(token: str) -> None:
    """Stop Telegram delivering to us. Called when a connection is removed."""
    call(token, "deleteWebhook", {"drop_pending_updates": True}, timeout=BACKGROUND_TIMEOUT)


def deep_link(username: str, payload: str) -> str:
    """A ``t.me`` deep link that opens the bot and sends ``/start <payload>``."""
    return f"https://t.me/{username.lstrip('@')}?start={payload}"


# ---------------------------------------------------------------------------
# Outbound: OutboundMessage -> Bot API calls
# ---------------------------------------------------------------------------


def wire_calls(chat_id: str, message: OutboundMessage) -> list[tuple[str, dict[str, Any]]]:
    """``(method, payload)`` pairs for one already-downgraded message.

    Pure: no HTTP, no database, no clock. That is what lets the send-payload
    snapshots be a table, and what lets a reader check a payload against
    Telegram's documentation without reading the send loop.

    ``message`` is expected to have been through
    :func:`apps.channels.downgrade.downgrade` already, so cards and galleries
    are gone and text is within the 4096 cap. The card/gallery branches below
    are a backstop for a caller that skipped that step, not a second renderer.
    """
    calls: list[tuple[str, dict[str, Any]]] = []
    for block in message.blocks:
        calls.extend(_block_calls(chat_id, block))
    if not calls:
        return []

    markup = _reply_markup(message)
    if markup is not None:
        # On the last call, so the keyboard arrives with the final bubble rather
        # than attached to an image the rest of the message then scrolls past.
        method, payload = calls[-1]
        calls[-1] = (method, {**payload, "reply_markup": markup})
    return calls


def _block_calls(chat_id: str, block: Any) -> list[tuple[str, dict[str, Any]]]:
    if isinstance(block, TextBlock):
        return _text_calls(chat_id, block.text)
    if isinstance(block, MediaBlock):
        return _media_calls(chat_id, block)
    if isinstance(block, CardBlock):
        return _text_calls(chat_id, _card_text(block.card))
    if isinstance(block, GalleryBlock):
        calls: list[tuple[str, dict[str, Any]]] = []
        for card in block.cards:
            calls.extend(_text_calls(chat_id, _card_text(card)))
        return calls
    return []


def _text_calls(chat_id: str, text: str) -> list[tuple[str, dict[str, Any]]]:
    """``sendMessage`` calls for ``text``, split if it is over the cap.

    Telegram rejects an empty ``text`` outright, so a blank block produces no
    call at all rather than a message that fails at the platform.
    """
    if not text.strip():
        return []
    return [("sendMessage", {"chat_id": chat_id, "text": part}) for part in split_text(text, MAX_TEXT_CHARS)]


def _card_text(card: Any) -> str:
    """A card as plain text. Only reached if a caller skipped the downgrader."""
    return "\n".join(part for part in (card.title, card.subtitle, card.url) if part)


def _media_calls(chat_id: str, block: MediaBlock) -> list[tuple[str, dict[str, Any]]]:
    """One media call, plus a follow-up message when the caption is too long.

    Telegram caps a caption at 1024 characters against 4096 for a message, and
    the downgrade renderer only knows about the latter. Truncating would lose
    the end of something an author wrote; sending the caption as its own message
    afterwards keeps all of it, in order, and is what a person would do.
    """
    entry = _MEDIA_METHODS.get(block.kind)
    if entry is None or not block.url:
        # An unsupported kind should already have become text upstream; a media
        # block with no address cannot be sent at all. Either way the caption is
        # the only thing left worth delivering.
        return _text_calls(chat_id, "\n".join(part for part in (block.caption, block.url) if part))

    method, key = entry
    payload: dict[str, Any] = {"chat_id": chat_id, key: block.url}
    caption = block.caption
    if not caption:
        return [(method, payload)]
    if len(caption) <= MAX_CAPTION_CHARS:
        payload["caption"] = caption
        return [(method, payload)]
    return [(method, payload), *_text_calls(chat_id, caption)]


def _reply_markup(message: OutboundMessage) -> dict[str, Any] | None:
    """The keyboard for this message, or None.

    Telegram allows **one** ``reply_markup`` per message, and the two kinds are
    not interchangeable: an inline keyboard rides on the message and answers
    with ``callback_data``, a reply keyboard replaces the contact's keyboard and
    answers with the button's label as ordinary text.

    So: buttons win, because they are the ones that carry ids. Quick replies
    alongside them are folded into the same inline keyboard rather than dropped
    — a :class:`~apps.channels.events.QuickReply` comes back as
    ``EventPayload.button_id`` exactly like a postback button does, so the
    semantics survive the change of clothes. Quick replies **alone** get the
    one-time reply keyboard SPEC §6.2 asks for.
    """
    if message.buttons:
        rows = [row for row in (_inline_button(item, message.node_id) for item in _pressable(message)) if row]
        return {"inline_keyboard": [[row] for row in rows]} if rows else None
    if message.quick_replies:
        keys = [{"text": _label(item)} for item in message.quick_replies if _label(item)]
        return (
            {"keyboard": [[key] for key in keys], "one_time_keyboard": True, "resize_keyboard": True} if keys else None
        )
    return None


def _pressable(message: OutboundMessage) -> list[Button]:
    """Buttons plus any quick replies riding in the inline keyboard with them."""
    extra = [Button(id=item.id, label=item.label) for item in message.quick_replies]
    if extra:
        logger.debug(
            "Telegram: %s quick repl(ies) sent as inline buttons because the message also has buttons.",
            len(extra),
        )
    return [*message.buttons, *extra]


def _inline_button(button: Button, node_id: str) -> dict[str, Any] | None:
    """One inline keyboard button, or None when it cannot be represented."""
    label = _label(button)
    if not label:
        return None
    if button.is_url:
        return {"text": label, "url": button.url}
    data = _callback_data(node_id, button.id)
    if data is None:
        return None
    return {"text": label, "callback_data": data}


def _label(item: Button | QuickReply) -> str:
    """Telegram rejects an empty button label; fall back to the id."""
    return (item.label or item.id).strip()


def _callback_data(node_id: str, button_id: str) -> str | None:
    """SPEC §6.2's ``node_id:button_id``, within Telegram's 64-byte cap.

    The graph schema constrains both ids to ``[A-Za-z0-9_-]{1,64}``
    (``apps.flows.schema.handles.HANDLE_PATTERN``), so neither can contain a
    colon and :func:`_button_id` can split on the first one. Together they can
    still exceed 64 bytes, and then the node id is what goes: it is
    decoration — the engine matches a press on the button id against the
    *waiting* node's handles, and a contact has one live execution — while the
    button id is the part that has to survive.

    None means the button cannot be sent at all. Only reachable for an id that
    is over 64 bytes on its own, which the schema does not permit and an inbox
    or API caller building an ``OutboundMessage`` by hand could still manage.
    """
    if not button_id:
        return None
    combined = f"{node_id}:{button_id}" if node_id else button_id
    if len(combined.encode("utf-8")) <= MAX_CALLBACK_DATA_BYTES:
        return combined
    if len(button_id.encode("utf-8")) <= MAX_CALLBACK_DATA_BYTES:
        logger.debug("Telegram: callback_data for %r dropped its node prefix to fit 64 bytes.", button_id)
        return button_id
    logger.warning(
        "Telegram: button id is over %s bytes and cannot be sent as callback_data; "
        "the button was left out of the keyboard.",
        MAX_CALLBACK_DATA_BYTES,
    )
    return None


# ---------------------------------------------------------------------------
# Inbound: update -> NormalizedEvent
# ---------------------------------------------------------------------------


def _text(value: Any, limit: int = MAX_INBOUND_TEXT_CHARS) -> str:
    """A bounded string, or "". Every inbound field goes through this."""
    return value[:limit] if isinstance(value, str) else ""


def _chat_id(container: Any) -> str:
    """``chat.id`` as a string, or "" when the shape is not what it should be.

    Telegram sends chat ids as JSON numbers. ``bool`` is excluded explicitly
    because it is an ``int`` in Python and ``str(True)`` is a chat id nobody has.
    """
    if not isinstance(container, dict):
        return ""
    raw = container.get("id")
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        return ""
    return str(raw)[:200]


def _timestamp(raw: Any) -> Any:
    """The update's own time, or now.

    A wrong clock on an event is a cosmetic problem; refusing the event because
    its ``date`` was a string is a lost message. ``fromtimestamp`` raises on
    values outside the platform's range, which is exactly what a hostile payload
    would send.
    """
    if isinstance(raw, int) and not isinstance(raw, bool):
        try:
            return datetime.fromtimestamp(raw, UTC)
        except (OverflowError, OSError, ValueError):
            pass
    return timezone.now()


def _sender_extra(sender: Any, chat: Any) -> dict[str, Any]:
    """Display detail worth keeping. Attacker-controlled: escape on render."""
    extra: dict[str, Any] = {}
    if isinstance(sender, dict):
        for key in ("username", "first_name", "last_name", "language_code"):
            value = _text(sender.get(key), MAX_EXTRA_CHARS)
            if value:
                extra[key] = value
    if isinstance(chat, dict):
        chat_type = _text(chat.get("type"), MAX_EXTRA_CHARS)
        if chat_type:
            extra["chat_type"] = chat_type
    return extra


def _media_ids(message: dict[str, Any]) -> tuple[str, ...]:
    """``file_id``s for whatever media this message carries.

    Deliberately **not** ``EventPayload.attachments``: that field is documented
    as URLs, and a Telegram ``file_id`` is not one — turning it into a URL needs
    a ``getFile`` call and produces a link that expires in an hour. Storing an
    id and resolving it on demand is the honest shape, and it keeps us clear of
    SECURITY-BASELINE §6, which forbids fetching platform-supplied URLs
    server-side until the SSRF guard lands.
    """
    ids: list[str] = []
    for key, _kind in _MEDIA_FIELDS:
        value = message.get(key)
        if isinstance(value, list):
            # A photo arrives as every size Telegram made; the last is the
            # largest, and one id per photo is what a consumer wants.
            sizes = [_text(item.get("file_id"), 200) for item in value if isinstance(item, dict)]
            usable = [item for item in sizes if item]
            if usable:
                ids.append(usable[-1])
        elif isinstance(value, dict):
            file_id = _text(value.get("file_id"), 200)
            if file_id:
                ids.append(file_id)
    return tuple(ids)


def _contact_text(contact: Any) -> str:
    """A shared contact card, as text. SPEC §7.2 has no richer place for it."""
    if not isinstance(contact, dict):
        return ""
    name = " ".join(
        part for part in (_text(contact.get("first_name"), 100), _text(contact.get("last_name"), 100)) if part
    )
    phone = _text(contact.get("phone_number"), 50)
    detail = ", ".join(part for part in (name, phone) if part)
    return f"Shared a contact: {detail}" if detail else ""


def _location_text(location: Any) -> str:
    """A shared location, as text."""
    if not isinstance(location, dict):
        return ""
    latitude = location.get("latitude")
    longitude = location.get("longitude")
    if isinstance(latitude, bool) or isinstance(longitude, bool):
        return ""
    if not isinstance(latitude, (int, float)) or not isinstance(longitude, (int, float)):
        return ""
    return f"Shared a location: {latitude}, {longitude}"


def _button_id(data: str) -> str:
    """The button half of ``callback_data``.

    Split on the **first** colon, because that is where :func:`_callback_data`
    put it and because a node id can never contain one. Data with no colon is a
    bare button id, which is what the length fallback produces.
    """
    return data.split(":", 1)[-1]


# ---------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------


class TelegramAdapter(Adapter):
    """SPEC §6.2, implemented against SPEC §6.1's interface."""

    platform = Platform.TELEGRAM.value
    capabilities = _CAPABILITIES
    webhook_content = "json"

    # -- inbound ------------------------------------------------------------

    def resolve_connection(self, request: "HttpRequest", raw_body: bytes) -> ChannelConnection | None:
        """Which bot this delivery is for, from the header secret.

        Telegram gives a deployment one ``/webhooks/telegram/`` URL and no way
        to put an id in it, so the secret is the only thing distinguishing two
        bots. ``resolve_by_webhook_secret`` matches on the queryable HMAC — an
        encrypted column cannot be filtered — and the endpoint checks the
        platform and the status of whatever comes back before trusting it
        (``views_webhooks._usable``).
        """
        return ChannelConnection.resolve_by_webhook_secret(request.headers.get(SECRET_HEADER, ""))

    def verify_webhook(self, request: "HttpRequest", connection: ChannelConnection) -> bool:
        """Constant-time compare of the header against the stored secret.

        Deliberately a second check even though :meth:`resolve_connection` just
        found this row *by* the same value: the per-connection routes and any
        future caller can hand us a connection we did not resolve, and a
        ``verify_webhook`` that trusted its argument would be a hole the day
        someone adds one. ``verify_webhook_secret`` compares fixed-length HMACs
        with ``secrets.compare_digest``.
        """
        return connection.verify_webhook_secret(request.headers.get(SECRET_HEADER, ""))

    def parse_events(self, request: "HttpRequest", connection: ChannelConnection) -> list[NormalizedEvent]:
        """One Telegram update becomes at most one normalized event.

        Defensive by contract (SECURITY-BASELINE §2): every value here was typed
        by a stranger. Nothing raises, nothing assumes a key exists, everything
        is length-bounded, and an update we do not understand produces no event
        rather than a half-populated one.
        """
        payload = security.json_payload(request) or {}
        update_id = payload.get("update_id")
        if isinstance(update_id, bool) or not isinstance(update_id, int):
            # Every genuine update has one, and it is the deduplication key
            # (SPEC §7.1 step 2). Without it we would have to synthesise an id
            # from content, and two identical "hi" messages would collide.
            logger.info("Telegram delivery on connection %s carried no usable update_id.", connection.pk)
            return []

        provider_event_id = f"tg:{update_id}"
        message = payload.get("message")
        if isinstance(message, dict):
            return self._from_message(connection, message, provider_event_id, payload)
        callback_query = payload.get("callback_query")
        if isinstance(callback_query, dict):
            return self._from_callback_query(connection, callback_query, provider_event_id, payload)
        return []

    def _from_message(
        self,
        connection: ChannelConnection,
        message: dict[str, Any],
        provider_event_id: str,
        raw: dict[str, Any],
    ) -> list[NormalizedEvent]:
        chat_id = _chat_id(message.get("chat"))
        if not chat_id:
            return []

        text = _text(message.get("text")) or _text(message.get("caption"))
        media_ids = _media_ids(message)
        if not text and not media_ids:
            # contact and location have no text of their own; SPEC §7.2 has no
            # field for either, so they arrive as the sentence a person would
            # have typed. Anything still empty after this is an update type we
            # do not carry (a service message, a poll) and is dropped.
            text = _contact_text(message.get("contact")) or _location_text(message.get("location"))
            if not text:
                return []

        extra = _sender_extra(message.get("from"), message.get("chat"))
        timestamp = _timestamp(message.get("date"))

        ref = _start_ref(text)
        if ref is not None:
            if ref:
                # SPEC §10's ref_url trigger: `t.me/<bot>?start=<ref>`.
                return [
                    NormalizedEvent(
                        type=EventType.REFERRAL,
                        connection=connection,
                        platform_user_id=chat_id,
                        provider_event_id=provider_event_id,
                        timestamp=timestamp,
                        payload=EventPayload(ref=ref[:MAX_REF_CHARS], extra=extra),
                        raw=raw,
                    )
                ]
            # Bare /start. SPEC §10 calls this the *welcome* trigger, but there
            # is no `welcome` member of EventType — welcome is a trigger type,
            # not an event type — and the contact really did send a message, so
            # it stays a message and announces itself in `extra`. L4-A's welcome
            # trigger keys off that flag rather than string-matching the text,
            # and L5-B's Messenger adapter has the same job with `get_started`.
            extra = {**extra, "command": "start"}

        return [
            NormalizedEvent(
                type=EventType.MESSAGE,
                connection=connection,
                platform_user_id=chat_id,
                provider_event_id=provider_event_id,
                timestamp=timestamp,
                payload=EventPayload(text=text, media_ids=media_ids, extra=extra),
                raw=raw,
            )
        ]

    def _from_callback_query(
        self,
        connection: ChannelConnection,
        query: dict[str, Any],
        provider_event_id: str,
        raw: dict[str, Any],
    ) -> list[NormalizedEvent]:
        # Bounded by what we could ever have sent: anything longer was not
        # produced by _callback_data and will not match a handle either way.
        data = _text(query.get("data"), MAX_CALLBACK_DATA_BYTES)
        source_message = query.get("message")
        chat_id = _chat_id(source_message.get("chat") if isinstance(source_message, dict) else None)
        if not chat_id:
            # An inline-mode press has no `message`. We do not enable inline
            # mode, but the sender is still the person, and their id is the chat
            # id for a private bot conversation.
            chat_id = _chat_id(query.get("from"))
        if not chat_id or not data:
            return []

        self._answer_callback_query(connection, _text(query.get("id"), 200))

        return [
            NormalizedEvent(
                type=EventType.POSTBACK,
                connection=connection,
                platform_user_id=chat_id,
                provider_event_id=provider_event_id,
                timestamp=_timestamp(source_message.get("date") if isinstance(source_message, dict) else None),
                payload=EventPayload(
                    button_id=_button_id(data),
                    extra={**_sender_extra(query.get("from"), None), "callback_data": data},
                ),
                raw=raw,
            )
        ]

    def _answer_callback_query(self, connection: ChannelConnection, query_id: str) -> None:
        """Clear the spinner on the pressed button (SPEC §6.2).

        This is an outbound call on the inbound path, which is unusual enough to
        justify: Telegram leaves a progress indicator on the button until the
        bot answers, and the only other way to answer is to put the method in
        the webhook *response* body — which this framework's endpoint owns and
        does not delegate. One extra request, only for button presses, inside
        SPEC §7.1's budget.

        Failure is swallowed on purpose. The spinner clearing is cosmetic; the
        press is not, and ``views_webhooks._parse_events`` drops the whole
        delivery if this method raises.
        """
        if not query_id:
            return
        try:
            call(bot_token(connection), "answerCallbackQuery", {"callback_query_id": query_id})
        except Exception:
            logger.warning("Telegram: could not answer a callback query on connection %s.", connection.pk)

    # -- outbound -----------------------------------------------------------

    def send(self, connection: ChannelConnection, identity: Any, outbound: OutboundMessage) -> SendResult:
        """Deliver one message, downgrading it first (SPEC §6.1).

        The downgrade can turn one abstract message into several — a gallery
        becomes one message per card — and they go in order. The result reports
        the **last** provider id: it is the message the contact is looking at,
        and the one a reply or a delivery receipt will reference.

        **A multi-part send is not atomic, and cannot be here.** If the third of
        three calls fails, the first two have already arrived, and the retry
        (SPEC §9.4 keys idempotency on the *message row*, of which there is one)
        sends all three again — so the contact sees the first two twice. The fix
        is per-part progress on the message row, which is a schema change and a
        decision for every adapter rather than this one; Layer 5's carousels hit
        the same case. Until then the behaviour is: duplicate rather than drop,
        which is the right direction for a message a flow author intended to
        send, and it is written down here rather than discovered in production.
        """
        chat_id = str(getattr(identity, "platform_user_id", "") or "")
        if not chat_id:
            return SendResult(status=SendStatus.FAILED, error="no_chat_id")

        token = bot_token(connection)
        rendered = downgrade(outbound, self.capabilities)
        calls: list[tuple[str, dict[str, Any]]] = []
        for message in rendered.messages:
            calls.extend(wire_calls(chat_id, message))
        if not calls:
            # Nothing sendable survived. Reported rather than silently counted
            # as sent, so contract 1's message row says what happened.
            return SendResult(status=SendStatus.FAILED, error="empty_message")

        provider_message_id = ""
        for method, payload in calls:
            try:
                result = call(token, method, payload)
            except APIError as exc:
                self._handle_send_error(connection, chat_id, exc)
                raise
            if isinstance(result, dict):
                provider_message_id = _provider_message_id(result) or provider_message_id
        return SendResult(status=SendStatus.SENT, provider_message_id=provider_message_id)

    def _handle_send_error(self, connection: ChannelConnection, chat_id: str, exc: APIError) -> None:
        """Turn a 403 into an opt-out, then let the error carry on.

        Telegram answers 403 when the contact blocked the bot, deleted their
        account, or the chat is gone. All three mean the same thing operationally
        — never send here again — and continuing to try is what gets a bot
        rate-limited and then reported.

        The adapter does **not** write ``identity.opted_out_at`` itself: ROADMAP
        contract 3 reserves that field for the messaging facade and the ingest
        pipeline. So this raises the event the pipeline already knows how to
        apply and hands it to the same dispatch a webhook would
        (``apps.messaging.ingest._apply_opt_out``), which also lets L5-D's
        hard-optout hook and anything else on the seam see it.
        """
        if exc.status_code != 403:
            return
        logger.info("Telegram: connection %s can no longer reach a chat; recording an opt-out.", connection.pk)
        now = timezone.now()
        event = NormalizedEvent(
            type=EventType.OPT_OUT,
            connection=connection,
            platform_user_id=chat_id,
            # Timestamped rather than content-only: a contact who blocks, is
            # unblocked and blocks again is two events, not one duplicate.
            provider_event_id=f"tg:blocked:{chat_id}:{int(now.timestamp())}",
            timestamp=now,
            payload=EventPayload(extra={"reason": "forbidden"}),
        )
        try:
            channels_ingest.process_events(connection, (event,))
        except Exception:
            # The send failure is the thing the caller is waiting to hear about.
            logger.exception("Telegram: could not record an opt-out on connection %s.", connection.pk)

    def send_typing(self, connection: ChannelConnection, identity: Any) -> None:
        """``sendChatAction``. Cosmetic, so a failure is logged and swallowed."""
        chat_id = str(getattr(identity, "platform_user_id", "") or "")
        if not chat_id:
            return
        try:
            call(bot_token(connection), "sendChatAction", {"chat_id": chat_id, "action": "typing"})
        except APIError:
            logger.debug("Telegram: typing indicator failed on connection %s.", connection.pk)

    # `mark_seen` stays the base class's no-op: bots have no read receipts.

    # -- lifecycle ----------------------------------------------------------

    def on_disconnect(self, connection: ChannelConnection) -> None:
        """``deleteWebhook``, so a removed bot stops delivering to a dead URL."""
        delete_webhook(bot_token(connection))


def _provider_message_id(result: Any) -> str:
    """``message_id`` from a send result, as the string the column holds.

    Telegram sends it as a number and the ``Message.provider_message_id`` column
    is text, so the conversion is ours to make. A result without one leaves the
    previous value standing rather than blanking it — several calls make up one
    logical send, and only the ones that produced a message have an id.
    """
    raw = result.get("message_id")
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        return ""
    return str(raw)[:200]


def _start_ref(text: str) -> str | None:
    """The payload of a ``/start``, "" for a bare one, None if not a ``/start``.

    Three-valued because the caller has three cases to tell apart and the
    distinction between "no payload" and "not a start command" is the whole
    difference between SPEC §10's welcome and keyword triggers.
    """
    stripped = text.strip()
    if stripped == START_COMMAND:
        return ""
    if stripped.startswith(f"{START_COMMAND} "):
        return stripped[len(START_COMMAND) :].strip()
    return None


register_adapter(Platform.TELEGRAM, TelegramAdapter)
