"""WhatsApp Cloud API adapter (SPEC §6.5) — issue #19.

Written from :mod:`apps.channels.providers.telegram`, which says in its own
docstring that "a Layer-5 author copying this should be able to replace the
helpers and keep the class". That is what happened here: the helpers below are
all about WhatsApp — the webhook envelope, the ``wamid``, the interactive
shapes, the template components — while the HTTP mechanics, the timeout policy,
the ``429`` → :class:`RateLimitError` mapping and the "never put a URL in an
error message" rule are inherited from
:mod:`apps.channels.providers.base` and are not re-implemented.

Block downgrading is :func:`apps.channels.downgrade.downgrade`, shared. This
module declares no fallback ladder of its own; what it does is decide which
*native* shape a downgraded message becomes, which is genuinely WhatsApp's:
reply buttons, a list, or plain text.

--------------------------------------------------------------------------
The 24-hour window, and why there is no window code here
--------------------------------------------------------------------------

SPEC §6.5: outside 24 hours from the contact's last inbound message, only an
approved template goes out. **None of that is decided here.** The policy row in
:mod:`apps.channels.policy` says ``window_hours=24, outside_window="needs_template"``
and ``apps.messaging.compliance.can_send`` reads it as data; a send that reaches
:meth:`WhatsAppAdapter.send` has already been through that chokepoint, and one
that carries ``template_ref`` has already been told it may. Contract 4's promise
is that a platform costs a policy row and a module, and an ``if platform ==
"whatsapp"`` anywhere in ``apps/messaging/`` would be the thing that broke it.

What *is* here is everything that makes a template real: submitting it, polling
its review, and turning an approved one plus a bag of rendered variables into a
``template`` payload. The first two live in
:mod:`apps.channels.whatsapp_templates`, because they are rows and transitions
rather than platform mechanics.

--------------------------------------------------------------------------
Rate limits, and why there is no throttle in this file
--------------------------------------------------------------------------

Same answer Telegram gives, for the same reasons. The **global** limit is the
connection's token bucket (``apps.messaging.buckets``), configured by
``rate_default=20.0`` in :mod:`apps.channels.policy`. The **per-recipient**
limit falls out of the shape of the system: SPEC §9.6 serialises everything one
contact does behind an advisory lock, so two messages to the same number cannot
be in flight at once. A timer here would be a sleep held inside that lock. When
Meta disagrees anyway it answers ``429`` and the send pipeline reschedules.

--------------------------------------------------------------------------
Secrets
--------------------------------------------------------------------------

Two different credentials, with two different owners:

* the **system-user access token** is per connection, lives encrypted in
  ``connection.credentials``, and is the whole credential — anyone holding it
  can read and send as the business. It travels in an ``Authorization: Bearer``
  header rather than the ``access_token`` query parameter Graph also accepts,
  so it never appears in a URL that httpx logs at INFO. ``apps.common.logging``
  additionally scrubs Meta's ``EAA…`` token shape (SECURITY-BASELINE §5).
* the **app secret** that signs ``X-Hub-Signature-256`` is per deployment or per
  organization, and is resolved through
  :mod:`apps.credentials.resolution` — the same chain the rest of the product
  uses. It is never stored on the connection.
"""

import hashlib
import logging
import threading
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx
from django.utils import timezone

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
from apps.messaging.codes import Denial, Failure

if TYPE_CHECKING:
    from django.http import HttpRequest

logger = logging.getLogger(__name__)

__all__ = [
    "ACCESS_TOKEN_KEY",
    "API_VERSION",
    "PHONE_NUMBER_ID_KEY",
    "SIGNATURE_HEADER",
    "WABA_ID_KEY",
    "WhatsAppAdapter",
    "access_token",
    "call",
    "credentials_of",
    "poll_template_statuses",
    "store_credentials",
    "subscribe_app",
    "unsubscribe_app",
    "verify_phone_number",
    "wire_calls",
]

#: The Graph API root. A constant rather than a setting, for the reason
#: Telegram's ``API_ROOT`` gives: a configurable host on a path that carries a
#: business's messaging credential is an exfiltration primitive, not a feature.
GRAPH_ROOT = "https://graph.facebook.com"

#: Pinned, not floating. Meta versions the Cloud API and changes payload shapes
#: between versions; a deployment that silently followed ``latest`` would have
#: its parsers change under it on Meta's schedule rather than on ours.
API_VERSION = "v21.0"

#: The header Meta signs every delivery with (SPEC §6.3-6.5, one scheme for all
#: three Meta platforms). Verified over the raw body, before any JSON parsing.
SIGNATURE_HEADER = "X-Hub-Signature-256"

#: Where the three credential values sit inside ``connection.credentials``.
ACCESS_TOKEN_KEY = "access_token"  # noqa: S105 - a dict key, not a credential
WABA_ID_KEY = "waba_id"
PHONE_NUMBER_ID_KEY = "phone_number_id"

#: The webhook object and field this adapter answers for (SPEC §6.5). Anything
#: else in a delivery is ignored rather than guessed at — Meta sends account
#: updates, template status changes and flow events down the same subscription.
WEBHOOK_OBJECT = "whatsapp_business_account"
MESSAGES_FIELD = "messages"

_CAPABILITIES: Capabilities = capabilities_for(Platform.WHATSAPP)

#: Body text cap for an ordinary text message, from the shared table.
MAX_TEXT_CHARS = _CAPABILITIES.max_text_len

#: An **interactive** message caps its body far lower than a text message does —
#: 1024 against 4096 — and the downgrade renderer only knows about the latter.
#: Rather than truncate an author's words, anything over the cap is sent as
#: ordinary text messages first and the tail becomes the interactive body, which
#: is the same call ``telegram._media_calls`` makes for its 1024-character
#: caption.
MAX_INTERACTIVE_BODY_CHARS = 1024

#: Media captions are capped at 1024 too, and audio carries none at all.
MAX_CAPTION_CHARS = 1024

#: Reply-button and list-row limits. Meta rejects the whole message when any of
#: these is exceeded, so a label is shortened rather than the message lost.
MAX_BUTTON_TITLE_CHARS = 20
MAX_ROW_TITLE_CHARS = 24
MAX_ROW_DESCRIPTION_CHARS = 72
MAX_LIST_BUTTON_CHARS = 20
MAX_REPLY_ID_CHARS = 256

#: What the list's own opener button says. Meta requires one and gives it no
#: default; this is the label a contact taps to see the rows.
LIST_BUTTON_LABEL = "Choose"

#: What an interactive message says when the author gave it no text of its own.
#: Meta requires a body on both interactive shapes, so the alternative to a word
#: here is dropping the buttons — a media block plus buttons is exactly the case
#: that reaches it.
INTERACTIVE_BODY_FALLBACK = "Choose an option"

#: Longest inbound text we carry out of a parse. Meta's own cap is 4096;
#: ``apps.messaging.ingest`` bounds it again downstream, and this exists so a
#: hostile payload cannot make us hold an arbitrarily long string at all
#: (SECURITY-BASELINE §§2, 7).
MAX_INBOUND_TEXT_CHARS = MAX_TEXT_CHARS

#: Longest attacker-supplied display string kept in ``payload.extra``.
MAX_EXTRA_CHARS = 200

#: The width of the ``platform_user_id`` column an id has to fit. Longer ones
#: are hashed rather than cut — see :func:`_wa_identity`.
MAX_PLATFORM_ID_CHARS = 200

#: ``message.type`` values that carry a file, and the block kind each becomes.
#: Stickers are images and voice notes are audio as far as anything downstream
#: is concerned; giving each its own kind would mean every consumer learning
#: WhatsApp's vocabulary.
_MEDIA_TYPES: dict[str, str] = {
    "image": "image",
    "audio": "audio",
    "voice": "audio",
    "video": "video",
    "document": "file",
    "sticker": "image",
}

#: Block kind -> the message ``type`` and the payload key the media goes in.
_MEDIA_SEND_TYPES: dict[str, str] = {
    "image": "image",
    "audio": "audio",
    "video": "video",
    "file": "document",
}

#: Statuses ``apps.messaging.ingest`` will act on. Meta also emits ``deleted``
#: and ``warning``, which name no state on the delivery ladder.
_RECEIPT_STATUSES = frozenset({"sent", "delivered", "read", "failed"})

#: Meta's numeric error codes, mapped onto this product's own vocabulary.
#:
#: The mapping matters more than it looks. ``Message.error`` holds a registered
#: code and nothing else (``apps.messaging.codes``), because a provider's error
#: prose quotes the request that caused it — token included — and that column is
#: rendered in the inbox. An unregistered code would reach the operator as a raw
#: string with no sentence attached.
#:
#: 131047 is the one worth naming: Meta calls it "re-engagement message", and it
#: means the 24-hour window closed and the send needed a template. Reporting it
#: as ``needs_template`` makes an asynchronous failure read exactly like the
#: synchronous refusal ``can_send`` would have given, which is what an operator
#: comparing the two needs.
_ERROR_CODES: dict[str, str] = {
    "131047": Denial.NEEDS_TEMPLATE.value,
    "131048": Failure.RATE_LIMITED.value,
    "130429": Failure.RATE_LIMITED.value,
    "131056": Failure.RATE_LIMITED.value,
    "133015": Failure.PROVIDER_UNAVAILABLE.value,
    "131000": Failure.PROVIDER_UNAVAILABLE.value,
}

#: Anything not in the table above. A rejection we cannot name is still a
#: rejection, and the numeric code is preserved in the event's ``raw``.
_DEFAULT_ERROR_CODE = Failure.PROVIDER_REJECTED.value


# ---------------------------------------------------------------------------
# The Graph API client
# ---------------------------------------------------------------------------


#: The process-wide connection pool. Built lazily and never closed: it lives as
#: long as the process does, which is the point.
_POOL: httpx.Client | None = None

_POOL_LOCK = threading.Lock()


def _client() -> httpx.Client | None:
    """The HTTP client every Graph call goes through.

    A **pooled** client, for the reason ``telegram._client`` gives at length:
    ``request_json``'s default of one client per call costs a TCP connection and
    a TLS handshake per message sent, inside SPEC §7.1's 1.5 s inline budget.

    **This is also the test seam.** A test monkeypatches this function to return
    an ``httpx.Client(transport=httpx.MockTransport(...))`` and the whole module
    — the real error mapping, the real 429 handling, the real payload building —
    runs without a socket.
    """
    global _POOL
    if _POOL is None:
        with _POOL_LOCK:
            if _POOL is None:
                _POOL = httpx.Client(limits=httpx.Limits(max_keepalive_connections=8, max_connections=32))
    return _POOL


def credentials_of(connection: ChannelConnection) -> dict[str, str]:
    """This connection's stored WhatsApp credentials, or ``{}``.

    ``credentials`` is an encrypted column, so reading it can fail on a
    deployment whose key has changed. That is a configuration problem and not
    something a webhook or a send should turn into a 500, so it reads as "no
    credentials" and the caller fails the operation with a named error.
    """
    try:
        raw: Any = connection.credentials or {}
    except ValueError:
        logger.error("Connection %s: credentials could not be decrypted.", connection.pk)
        return {}
    if not isinstance(raw, dict):
        return {}
    return {key: value for key, value in raw.items() if isinstance(key, str) and isinstance(value, str)}


def access_token(connection: ChannelConnection) -> str:
    """The permanent system-user token stored on ``connection``, or ""."""
    return credentials_of(connection).get(ACCESS_TOKEN_KEY, "")


def store_credentials(
    connection: ChannelConnection,
    *,
    token: str,
    waba_id: str,
    phone_number_id: str,
) -> None:
    """Put the three values on the connection's encrypted credentials column.

    The only place they are written, mirroring ``telegram.store_bot_token``.
    Both exist so the encrypted-JSON column is reached through named functions:
    ``EncryptedJSONField`` subclasses ``TextField``, so django-stubs types the
    attribute as ``str`` and every direct ``connection.credentials = {...}`` is
    a type error even though the column holds JSON. One suppression here beats
    one at each call site.
    """
    connection.credentials = {  # type: ignore[assignment]
        ACCESS_TOKEN_KEY: token,
        WABA_ID_KEY: waba_id,
        PHONE_NUMBER_ID_KEY: phone_number_id,
    }


def call(
    token: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    method: str = "POST",
    params: dict[str, str] | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """One Graph API call. Returns the decoded body.

    Raises :class:`~apps.channels.providers.exceptions.APIError` — or
    :class:`~apps.channels.providers.exceptions.RateLimitError` on a 429 — via
    ``request_json``, which is also where the error *code* is lifted out of
    Meta's ``error.code`` and where the message names the host rather than the
    path.

    The token goes in an ``Authorization`` header rather than Graph's
    ``access_token`` query parameter. Both work; only one of them keeps the
    credential out of every URL httpx logs.

    ``timeout`` defaults to the inline budget SPEC §7.1 sets. Connect-time and
    template calls pass ``BACKGROUND_TIMEOUT``: nobody is waiting on a webhook
    for those.
    """
    if not token:
        raise APIError("This WhatsApp connection has no access token stored.")
    kwargs: dict[str, Any] = {"headers": {"Authorization": f"Bearer {token}"}}
    if payload is not None:
        kwargs["json"] = payload
    if params:
        kwargs["params"] = params
    return request_json(
        method,
        f"{GRAPH_ROOT}/{API_VERSION}/{path.lstrip('/')}",
        client=_client(),
        timeout=timeout,
        **kwargs,
    )


def verify_phone_number(token: str, phone_number_id: str) -> dict[str, Any]:
    """Prove the token works and learn which number it belongs to.

    The WhatsApp equivalent of Telegram's ``getMe``, and used the same way: the
    connect flow calls it **before** anything is written, so a token that does
    not work leaves no trace and the display name comes from Meta rather than
    from whatever the operator typed.
    """
    body = call(
        token,
        phone_number_id,
        method="GET",
        params={"fields": "id,display_phone_number,verified_name,quality_rating"},
        timeout=BACKGROUND_TIMEOUT,
    )
    if not isinstance(body.get("id"), str):
        raise APIError("Meta returned an unexpected phone number result")
    return body


def subscribe_app(token: str, waba_id: str) -> None:
    """Subscribe this app to the WABA's webhooks (SPEC §6.5).

    Without it Meta accepts the configuration and delivers nothing, which is a
    very quiet outage: the connection looks connected and no message ever
    arrives.
    """
    call(token, f"{waba_id}/subscribed_apps", {}, timeout=BACKGROUND_TIMEOUT)


def unsubscribe_app(token: str, waba_id: str) -> None:
    """Stop Meta delivering for this WABA. Called when a connection is removed."""
    call(token, f"{waba_id}/subscribed_apps", method="DELETE", timeout=BACKGROUND_TIMEOUT)


def poll_template_statuses() -> str | None:
    """L2-C's hourly template poll (SPEC §15).

    A delegate rather than the job itself. ``apps.queueing.housekeeping``
    reserved *this* dotted path for issue #19 before either module existed, and
    the work — reading rows, moving them between states, notifying an admin — is
    not platform mechanics, so it lives in
    :mod:`apps.channels.whatsapp_templates`. Keeping the reserved name pointing
    at something real is what makes the sweep pick it up with no registration
    line anywhere.

    Imported inside the function because that module reaches into notifications
    and the ORM, and this one is imported from ``AppConfig.ready``.
    """
    from apps.channels import whatsapp_templates

    return whatsapp_templates.poll_pending()


# ---------------------------------------------------------------------------
# Outbound: OutboundMessage -> Cloud API payloads
# ---------------------------------------------------------------------------


def wire_calls(to: str, message: OutboundMessage) -> list[dict[str, Any]]:
    """The ``POST /<phone_number_id>/messages`` bodies for one message.

    Pure: no HTTP, no database, no clock. That is what lets the send-payload
    snapshots be a table, and what lets a reader check a payload against Meta's
    documentation without reading the send loop.

    ``message`` is expected to have been through
    :func:`apps.channels.downgrade.downgrade` already, so galleries and cards
    are gone, URL buttons are inlined and text is within the 4096 cap. The card
    and gallery branches below are a backstop for a caller that skipped that
    step, not a second renderer.

    **A template send is the whole message.** When ``template_ref`` is set
    nothing else goes on the wire: a template *is* the message Meta approved,
    and appending the blocks beside it would send the same words twice — once
    inside the window's rules and once outside them. The engine renders the
    template's own copy into that text block, so the inbox thread shows what the
    contact saw; the block is for the thread, not for the wire.

    Reached per *message*, so a caller that downgraded first would call it once
    per downgraded message and send the template that many times.
    :meth:`WhatsAppAdapter._payloads` is what stops that, and is where a real
    send goes.
    """
    if message.template_ref:
        return [_template_payload(to, message)]

    payloads: list[dict[str, Any]] = []
    for block in message.blocks:
        payloads.extend(_block_payloads(to, block))

    interactive = _interactive_action(message)
    if interactive is None:
        return payloads
    return _with_interactive(to, payloads, interactive)


def _block_payloads(to: str, block: Any) -> list[dict[str, Any]]:
    if isinstance(block, TextBlock):
        return _text_payloads(to, block.text)
    if isinstance(block, MediaBlock):
        return _media_payloads(to, block)
    if isinstance(block, CardBlock):
        return _text_payloads(to, _card_text(block.card))
    if isinstance(block, GalleryBlock):
        payloads: list[dict[str, Any]] = []
        for card in block.cards:
            payloads.extend(_text_payloads(to, _card_text(card)))
        return payloads
    return []


def _envelope(to: str, kind: str) -> dict[str, Any]:
    """The four keys every Cloud API send carries."""
    return {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to, "type": kind}


def _text_payloads(to: str, text: str) -> list[dict[str, Any]]:
    """Text messages for ``text``, split if it is over the cap.

    Meta rejects an empty body outright, so a blank block produces no call at
    all rather than a message that fails at the platform.

    ``preview_url`` stays False. Turning it on would make Meta fetch whatever
    link a flow author (or a contact-supplied placeholder) put in the text and
    render a preview card from it — a server-side fetch of an arbitrary URL,
    performed by somebody else's infrastructure, for content nobody reviewed.
    """
    if not text.strip():
        return []
    return [
        {**_envelope(to, "text"), "text": {"body": part, "preview_url": False}}
        for part in split_text(text, MAX_TEXT_CHARS)
    ]


def _card_text(card: Any) -> str:
    """A card as plain text. Only reached if a caller skipped the downgrader."""
    return "\n".join(part for part in (card.title, card.subtitle, card.url) if part)


def _media_payloads(to: str, block: MediaBlock) -> list[dict[str, Any]]:
    """One media message, plus a follow-up when the caption will not fit.

    Meta caps a caption at 1024 characters against 4096 for a text body, and
    audio carries no caption at all. Truncating would lose the end of something
    an author wrote; sending the caption as its own message afterwards keeps all
    of it, in order, and is what a person would do.
    """
    kind = _MEDIA_SEND_TYPES.get(block.kind)
    if kind is None or not block.url:
        # An unsupported kind should already have become text upstream, and a
        # media block with no address cannot be sent at all. Either way the
        # caption is the only thing left worth delivering.
        return _text_payloads(to, "\n".join(part for part in (block.caption, block.url) if part))

    media: dict[str, Any] = {"link": block.url}
    caption = block.caption
    # Audio is the exception Meta makes: no caption field exists on it.
    if caption and kind != "audio" and len(caption) <= MAX_CAPTION_CHARS:
        media["caption"] = caption
        caption = ""
    payload = {**_envelope(to, kind), kind: media}
    return [payload, *_text_payloads(to, caption)] if caption else [payload]


def _interactive_action(message: OutboundMessage) -> dict[str, Any] | None:
    """The ``interactive`` shape this message wants, or None.

    WhatsApp offers two, and they are not interchangeable: ``button`` shows up
    to three reply buttons under the message, ``list`` shows an opener that
    reveals up to ten rows. One message may carry one of them.

    So: **buttons win**, because a flow author who declared buttons asked for
    the shape a contact can see without tapping twice. Quick replies alone get
    the list.

    A downgraded message never carries both: ``interaction_is_exclusive`` on
    this platform's capability row tells the shared renderer that the two kinds
    compete for one control set, so quick replies arriving beside buttons are
    already numbered text by the time this runs. The fold below is the backstop
    for an ``OutboundMessage`` built by hand — a
    :class:`~apps.channels.events.QuickReply` comes back as
    ``EventPayload.button_id`` exactly like a postback button does, so the
    semantics survive the change of clothes — and if even that overflows, it
    says so rather than dropping the overflow in silence.
    """
    if message.buttons:
        rows = [row for row in (_reply_button(item) for item in _pressable(message)) if row]
        if len(rows) > _CAPABILITIES.max_buttons:
            # Unreachable for a downgraded message; loud rather than silent,
            # because the alternative is a contact who cannot answer and a node
            # waiting on a handle nothing will ever fire.
            logger.warning(
                "WhatsApp: %s control(s) on one message exceed the %s reply buttons it can show; "
                "the message was not downgraded first and the extras cannot be delivered.",
                len(rows),
                _CAPABILITIES.max_buttons,
            )
        return {"type": "button", "action": {"buttons": rows[: _CAPABILITIES.max_buttons]}} if rows else None
    if message.quick_replies:
        rows = [row for row in (_list_row(item) for item in message.quick_replies) if row]
        rows = rows[: _CAPABILITIES.max_quick_replies]
        if not rows:
            return None
        return {
            "type": "list",
            "action": {"button": LIST_BUTTON_LABEL, "sections": [{"title": LIST_BUTTON_LABEL, "rows": rows}]},
        }
    return None


def _pressable(message: OutboundMessage) -> list[Button]:
    """Buttons plus any quick replies riding in the reply-button set with them."""
    extra = [Button(id=item.id, label=item.label) for item in message.quick_replies]
    if extra:
        logger.debug(
            "WhatsApp: %s quick repl(ies) sent as reply buttons because the message also has buttons.",
            len(extra),
        )
    return [*message.buttons, *extra]


def _reply_button(button: Button) -> dict[str, Any] | None:
    """One reply button, or None when it cannot be represented.

    A URL button never reaches here: ``url_buttons`` is False for this platform,
    so the shared renderer has already inlined it into the text as
    ``label: url``. The check stays because an ``OutboundMessage`` built by hand
    is not obliged to have been downgraded, and a URL button silently sent as a
    postback would come back matching no handle.

    An over-long id is **refused, not truncated**, which is the call
    ``telegram._callback_data`` makes for the same situation and for the same
    reason: a trimmed id is sent happily, tapped happily, and comes back
    matching no handle on the waiting node, so the press is silently swallowed.
    A button left out of the set is at least visible. The title is a different
    matter — it is decoration, and clipping it loses nothing a contact needs.
    """
    label = _label(button)
    if not label or button.is_url or not button.id:
        return None
    if len(button.id) > MAX_REPLY_ID_CHARS:
        logger.warning(
            "WhatsApp: button id is over %s characters and cannot be sent; it was left out of the set.",
            MAX_REPLY_ID_CHARS,
        )
        return None
    return {"type": "reply", "reply": {"id": button.id, "title": label[:MAX_BUTTON_TITLE_CHARS]}}


def _list_row(item: QuickReply) -> dict[str, Any] | None:
    """One list row, or None when it cannot be represented.

    The label is **shortened, not dropped**, when it is over the 24-character
    row limit: Meta rejects the whole message on an over-long title, so the
    choice is a clipped option or no message at all. The full text goes in the
    row's description, which WhatsApp shows underneath, so nothing an author
    wrote is actually lost.
    """
    label = _label(item)
    if not label or not item.id:
        return None
    if len(item.id) > MAX_REPLY_ID_CHARS:
        # Refused rather than trimmed, for the reason _reply_button gives.
        logger.warning(
            "WhatsApp: list row id is over %s characters and cannot be sent; the row was left out.",
            MAX_REPLY_ID_CHARS,
        )
        return None
    row: dict[str, Any] = {"id": item.id, "title": label[:MAX_ROW_TITLE_CHARS]}
    if len(label) > MAX_ROW_TITLE_CHARS:
        row["description"] = label[:MAX_ROW_DESCRIPTION_CHARS]
    return row


def _label(item: Button | QuickReply) -> str:
    """Meta rejects an empty button label; fall back to the id."""
    return (item.label or item.id).strip()


def _with_interactive(to: str, payloads: list[dict[str, Any]], action: dict[str, Any]) -> list[dict[str, Any]]:
    """Attach ``action`` to the message, turning its last text into the body.

    An interactive message carries its own body, and that body caps at 1024
    characters where a text message caps at 4096 — a limit the shared renderer
    does not know about, because it is a property of this one shape rather than
    of the platform. So the last text payload is reclaimed as the interactive
    body when it fits, and split when it does not: the leading parts go as
    ordinary text messages and the tail becomes the body, in order.

    Only the **last** payload is considered, so a message whose text comes
    before its image keeps that text as its own bubble and the buttons attach to
    the image. That is deliberate and it is what Telegram does: a keyboard
    belongs on the final bubble rather than on something the rest of the message
    scrolls past.

    A message with no text there still needs a body — Meta requires one on both
    interactive shapes — so it gets :data:`INTERACTIVE_BODY_FALLBACK` rather
    than losing its buttons.
    """
    body = ""
    if payloads and payloads[-1].get("type") == "text":
        body = str(payloads.pop()["text"]["body"])

    parts = split_text(body, MAX_INTERACTIVE_BODY_CHARS) if body else []
    if len(parts) > 1:
        # Everything but the tail goes back as ordinary text, keeping the order
        # the author wrote it in.
        payloads.extend(_text_payloads(to, "\n\n".join(parts[:-1])))
    tail = parts[-1] if parts else INTERACTIVE_BODY_FALLBACK

    payloads.append({**_envelope(to, "interactive"), "interactive": {**action, "body": {"text": tail}}})
    return payloads


def _template_payload(to: str, message: OutboundMessage) -> dict[str, Any]:
    """A ``template`` send, built from the reference and the rendered variables.

    Deliberately built from ``template_ref`` and ``template_variables`` **alone**
    — no database read. A retry rebuilds this ``OutboundMessage`` from the stored
    row hours later (``apps.messaging.rendering.outbound_from_body``), and a
    payload that needed a row to exist would be a retry that stops working the
    moment somebody deletes a template. It is also what keeps this function
    pure, and therefore snapshot-testable.
    """
    name, _, language = (message.template_ref or "").partition("/")
    payload: dict[str, Any] = {
        **_envelope(to, "template"),
        "template": {"name": name, "language": {"code": language or "en_US"}},
    }
    components = _template_components(message.template_variables)
    if components:
        payload["template"]["components"] = components
    return payload


def _template_components(variables: tuple[tuple[str, str], ...]) -> list[dict[str, Any]]:
    """``(slot, value)`` pairs to Meta's ``components`` list.

    The slot vocabulary is the platform-neutral one
    :class:`~apps.channels.events.OutboundMessage` documents — ``header.1``,
    ``body.2``, ``button.0.1`` — and this is the only place that knows what those
    mean to WhatsApp.

    **Meta binds parameters by position, not by name**, so ``{{2}}`` is whatever
    the second parameter happens to be. That makes two things load-bearing, and
    both are handled in :func:`_parameters` rather than here:

    * a **gap** must be filled. Supplying only ``body.2`` and emitting one
      parameter delivers that value in ``{{1}}``'s place — the contact reads the
      wrong thing and nothing anywhere reports a problem;
    * a **repeat** must collapse. Two entries for ``body.1`` emitted as two
      parameters make the count disagree with the template's placeholder count,
      and Meta refuses the whole message.

    So the slots are collected into ``{number: value}`` — last write wins, which
    is what makes a repeat harmless — and rendered as a contiguous run.

    A slot that does not parse is skipped. These strings are assembled by the
    flow engine rather than typed by a stranger, so a malformed one is a bug
    here — but it must not take a send down.
    """
    header: dict[int, str] = {}
    body: dict[int, str] = {}
    buttons: dict[int, dict[int, str]] = {}

    for slot, value in variables:
        parts = slot.split(".")
        if parts[0] == "header" and len(parts) == 2 and parts[1].isdigit():
            header[int(parts[1])] = value
        elif parts[0] == "body" and len(parts) == 2 and parts[1].isdigit():
            body[int(parts[1])] = value
        elif parts[0] == "button" and len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
            buttons.setdefault(int(parts[1]), {})[int(parts[2])] = value

    components: list[dict[str, Any]] = []
    if header:
        components.append({"type": "header", "parameters": _parameters(header)})
    if body:
        components.append({"type": "body", "parameters": _parameters(body)})
    for index in sorted(buttons):
        components.append(
            {
                "type": "button",
                "sub_type": "url",
                "index": str(index),
                "parameters": _parameters(buttons[index]),
            }
        )
    return components


def _parameters(slots: dict[int, str]) -> list[dict[str, str]]:
    """``{1: "a", 3: "c"}`` as the contiguous run Meta reads positionally.

    Numbering starts at ``{{1}}`` and runs to the highest slot supplied, with a
    missing one filled by the empty string. An empty placeholder is visibly
    wrong to whoever reads the message; a *shifted* one is not, and shifting is
    what skipping a gap would do — see :func:`_template_components`.

    A slot numbered zero or below cannot be a Meta placeholder and is dropped
    rather than allowed to set the length of the run.
    """
    usable = {number: value for number, value in slots.items() if number >= 1}
    if not usable:
        return []
    return [{"type": "text", "text": usable.get(number, "")} for number in range(1, max(usable) + 1)]


# ---------------------------------------------------------------------------
# Inbound: webhook payload -> NormalizedEvent
# ---------------------------------------------------------------------------


def _text(value: Any, limit: int = MAX_INBOUND_TEXT_CHARS) -> str:
    """A bounded string, or "". Every inbound field goes through this."""
    return value[:limit] if isinstance(value, str) else ""


def _changes(payload: Any) -> list[dict[str, Any]]:
    """Every ``messages`` change value in a delivery, defensively.

    One delivery legitimately carries several entries and several changes, and
    a Meta subscription also delivers fields this adapter does not answer for —
    account updates, template review outcomes, flow events. Those are filtered
    here rather than at each call site so a shape we do not understand costs
    nothing.
    """
    if not isinstance(payload, dict) or payload.get("object") != WEBHOOK_OBJECT:
        return []
    entries = payload.get("entry")
    if not isinstance(entries, list):
        return []
    values: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        changes = entry.get("changes")
        if not isinstance(changes, list):
            continue
        for change in changes:
            if not isinstance(change, dict) or change.get("field") != MESSAGES_FIELD:
                continue
            value = change.get("value")
            if isinstance(value, dict):
                values.append(value)
    return values


def _phone_number_id(value: dict[str, Any]) -> str:
    """``metadata.phone_number_id`` — which of our numbers this change is for.

    **Digits only, and that is a guard rather than tidiness.** This value comes
    straight off an unverified body — ``resolve_connection`` runs before the
    signature is checked, because Meta gives a deployment one URL and the
    connection can only be found by reading the payload — and it is then used as
    a queryset filter. A string carrying a NUL byte reaches psycopg, which
    refuses it with ``DataError`` rather than returning no rows, and that
    exception escapes ``resolve_connection`` as a 500 on the one endpoint
    strangers can reach: an unauthenticated denial-of-service primitive, and one
    that makes Meta retry the same body forever.

    A real phone number id is a decimal Graph id, so anything else cannot name a
    connection this deployment has and there is nothing to gain by asking the
    database about it. Rejecting the whole shape is stronger than scrubbing one
    character, because it is closed by construction rather than by remembering
    which characters are dangerous.
    """
    metadata = value.get("metadata")
    if not isinstance(metadata, dict):
        return ""
    candidate = _text(metadata.get("phone_number_id"), MAX_EXTRA_CHARS)
    return candidate if candidate.isdigit() else ""


def _wa_identity(raw: Any) -> str:
    """A ``wa_id`` as this product's ``platform_user_id``: E.164 with a ``+``.

    The ``+`` is added rather than assumed away, and that is the whole reason
    this adapter can link a WhatsApp contact to one captured over SMS. A
    ``wa_id`` is an E.164 number *without* the plus, and
    ``apps.common.addresses.normalize_phone`` deliberately refuses a bare string
    of digits — it will not guess a country code. Prefixing here turns an id
    that module would reject into the address it recognises, which is exactly
    what ``apps.messaging.identities``'s ``ADDRESS_PLATFORMS`` entry for this
    platform depends on.

    An absurdly long id is **hashed, not truncated**, the rule this codebase
    applies to every other identifier (``messaging.identities.bounded_key``,
    ``channels.views_webhooks._dedup_id``, ``telegram._private_chat_id``, all of
    which explain it at length). Truncating narrows an identity key without
    saying so, and two ids agreeing on their first 200 characters would become
    one person receiving another's conversation.
    """
    digits = raw if isinstance(raw, str) else ""
    digits = digits.strip().lstrip("+")
    if not digits or not digits.isdigit():
        return ""
    value = f"+{digits}"
    if len(value) <= MAX_PLATFORM_ID_CHARS:
        return value
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _timestamp(raw: Any) -> datetime:
    """The event's own time, or now.

    Meta sends epoch seconds **as a string**. A wrong clock on an event is a
    cosmetic problem; refusing the event because its timestamp was malformed is
    a lost message, so anything unparseable falls back to now.
    """
    if isinstance(raw, bool):
        return timezone.now()
    if isinstance(raw, str) and raw.isdigit():
        raw = int(raw)
    if isinstance(raw, int):
        try:
            return datetime.fromtimestamp(raw, UTC)
        except (OverflowError, OSError, ValueError):
            pass
    return timezone.now()


def _profile_names(value: dict[str, Any]) -> dict[str, str]:
    """``{wa_id: profile name}`` from a change's ``contacts`` array.

    Attacker-controlled display text: bounded here and escaped on render.
    """
    contacts = value.get("contacts")
    if not isinstance(contacts, list):
        return {}
    names: dict[str, str] = {}
    for contact in contacts:
        if not isinstance(contact, dict):
            continue
        identity = _wa_identity(contact.get("wa_id"))
        profile = contact.get("profile")
        name = _text(profile.get("name"), MAX_EXTRA_CHARS) if isinstance(profile, dict) else ""
        if identity and name:
            names[identity] = name
    return names


def _media_id(message: dict[str, Any], kind: str) -> str:
    """The media id on a message of type ``kind``.

    Deliberately **not** ``EventPayload.attachments``: that field is documented
    as URLs, and a Cloud API media id is not one — turning it into a URL needs a
    second Graph call and produces a link that expires. Storing the id and
    resolving it on demand is the honest shape, and it keeps us clear of
    SECURITY-BASELINE §6's rule about fetching platform-supplied URLs.
    """
    media = message.get(kind)
    return _text(media.get("id"), MAX_EXTRA_CHARS) if isinstance(media, dict) else ""


def _media_caption(message: dict[str, Any], kind: str) -> str:
    media = message.get(kind)
    return _text(media.get("caption")) if isinstance(media, dict) else ""


def _interactive_reply(message: dict[str, Any]) -> tuple[str, str]:
    """``(button_id, title)`` from an interactive reply, or ``("", "")``.

    Both shapes this adapter can send come back here — ``button_reply`` from a
    reply-button set and ``list_reply`` from a list — plus ``button``, which is
    what a *template's* quick-reply button sends and which carries a ``payload``
    rather than an id. All three become the same postback, because the engine
    matches on the handle and does not care which widget produced it.
    """
    interactive = message.get("interactive")
    if isinstance(interactive, dict):
        for key in ("button_reply", "list_reply"):
            reply = interactive.get(key)
            if isinstance(reply, dict):
                return _text(reply.get("id"), MAX_REPLY_ID_CHARS), _text(reply.get("title"), MAX_EXTRA_CHARS)
        return "", ""
    button = message.get("button")
    if isinstance(button, dict):
        return _text(button.get("payload"), MAX_REPLY_ID_CHARS), _text(button.get("text"), MAX_EXTRA_CHARS)
    return "", ""


def _button_id(data: str) -> str:
    """The button half of a ``node_id:button_id`` reply id.

    Split on the **first** colon, for the reason ``telegram._button_id`` gives:
    that is where the id was assembled, a node id can never contain a colon, and
    everything after it is the button id, colons and all. Data with no colon is
    accepted as a bare button id — a template's quick-reply payload is authored
    in Meta's console and has no node prefix at all.
    """
    return data.split(":", 1)[-1]


def _location_text(location: Any) -> str:
    """A shared location, as text. SPEC §7.2 has no richer place for it."""
    if not isinstance(location, dict):
        return ""
    latitude = location.get("latitude")
    longitude = location.get("longitude")
    if isinstance(latitude, bool) or isinstance(longitude, bool):
        return ""
    if not isinstance(latitude, (int, float)) or not isinstance(longitude, (int, float)):
        return ""
    return f"Shared a location: {latitude}, {longitude}"


def _contact_text(contacts: Any) -> str:
    """A shared contact card, as text."""
    if not isinstance(contacts, list) or not contacts:
        return ""
    first = contacts[0]
    if not isinstance(first, dict):
        return ""
    name = first.get("name")
    formatted = _text(name.get("formatted_name"), MAX_EXTRA_CHARS) if isinstance(name, dict) else ""
    phones = first.get("phones")
    phone = ""
    if isinstance(phones, list) and phones and isinstance(phones[0], dict):
        phone = _text(phones[0].get("phone"), 50)
    detail = ", ".join(part for part in (formatted, phone) if part)
    return f"Shared a contact: {detail}" if detail else ""


def _status_error(status: dict[str, Any]) -> str:
    """This product's code for whatever Meta said went wrong.

    Meta puts the detail in ``errors[].code`` plus prose in ``title`` and
    ``error_data.details``. Only the code is read: the prose quotes the request
    that caused the failure, and ``Message.error`` is rendered in the inbox
    (SECURITY-BASELINE §5). The prose survives in ``webhook_event_log.raw`` for
    an operator who needs it.
    """
    errors = status.get("errors")
    if not isinstance(errors, list):
        return _DEFAULT_ERROR_CODE
    for error in errors:
        if not isinstance(error, dict):
            continue
        code = error.get("code")
        if isinstance(code, bool) or not isinstance(code, (int, str)):
            continue
        return _ERROR_CODES.get(str(code), _DEFAULT_ERROR_CODE)
    return _DEFAULT_ERROR_CODE


# ---------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------


class WhatsAppAdapter(Adapter):
    """SPEC §6.5, implemented against SPEC §6.1's interface."""

    platform = Platform.WHATSAPP.value
    capabilities = _CAPABILITIES
    webhook_content = "json"

    # -- inbound ------------------------------------------------------------

    def resolve_connection(self, request: "HttpRequest", raw_body: bytes) -> ChannelConnection | None:
        """Which number this delivery is for, from ids inside the payload.

        SPEC §7.1 gives Meta one ``/webhooks/whatsapp/`` URL per deployment with
        no room for an id, so the connection can only be found by reading the
        body — which means parsing before the signature is verified. That is
        inherent to the URL shape rather than a choice made here, and the
        framework already bounds it: ``views_webhooks`` applies the JSON nesting
        cap as a byte scan **before** this method runs, and the size cap before
        that, so the parser an unauthenticated caller reaches is a bounded one.

        Cross-tenant by necessity — an inbound webhook has no session and
        therefore no workspace — so this is a deliberate, greppable
        ``.unscoped()`` (CONTRIBUTING). What bounds it is the platform filter
        plus ``views_webhooks._usable``, which re-checks platform and status
        before anything is trusted, and the signature check that follows.
        """
        for value in _changes(security.json_payload(request)):
            connection = self._connection_for(_phone_number_id(value))
            if connection is not None:
                return connection
        return None

    def _connection_for(self, phone_number_id: str) -> ChannelConnection | None:
        if not phone_number_id:
            return None
        return (
            # Cross-tenant by necessity: see resolve_connection.
            ChannelConnection.objects.unscoped()
            .filter(platform=Platform.WHATSAPP.value, external_id=phone_number_id)
            .first()
        )

    def verify_webhook(self, request: "HttpRequest", connection: ChannelConnection) -> bool:
        """``X-Hub-Signature-256`` over the raw body, constant time.

        The secret is the **Meta app secret**, not anything on the connection:
        Meta signs with the app's secret, and one app serves every number a
        deployment has connected. It is resolved through the standard chain
        (workspace override → organization → environment), so a workspace that
        brought its own Meta app is verified against its own secret.

        Fails closed on everything — no secret configured, no header, a wrong
        prefix, a non-hex digest — and every one of those is indistinguishable
        to the caller from a wrong signature, which is the point.
        """
        return security.verify_signature_header(
            secret=self._app_secret(connection),
            raw_body=request.body,
            header_value=request.headers.get(SIGNATURE_HEADER),
        )

    def _app_secret(self, connection: ChannelConnection) -> str:
        from apps.credentials.resolution import resolve_platform_credentials

        resolution = resolve_platform_credentials(Platform.WHATSAPP.value, workspace=connection.workspace)
        credentials = resolution.credentials or {}
        # Meta's own docs say app_secret while its OAuth endpoints say
        # client_secret; apps.credentials.models accepts both spellings, so both
        # are read here rather than picking one and failing on the other.
        for key in ("app_secret", "client_secret"):
            value = credentials.get(key)
            if isinstance(value, str) and value:
                return value
        logger.warning(
            "WhatsApp: no Meta app secret resolved for workspace %s, so no delivery can be verified. "
            "The credential chain only uses a level that is complete, so PLATFORM_WHATSAPP_APP_SECRET "
            "needs PLATFORM_WHATSAPP_APP_ID beside it (apps.credentials.models.REQUIRED_CREDENTIAL_KEYS).",
            connection.workspace_id,
        )
        return ""

    def parse_events(self, request: "HttpRequest", connection: ChannelConnection) -> list[NormalizedEvent]:
        """One delivery becomes zero or more normalized events.

        Defensive by contract (SECURITY-BASELINE §2): every value here was typed
        by a stranger or assembled by Meta from one. Nothing raises, nothing
        assumes a key exists, everything is length-bounded, and a shape we do
        not understand produces no event rather than a half-populated one.

        A delivery can span several of *our* numbers, so each change carries its
        own connection. ``views_webhooks._record`` groups by it and drops any
        that is not the verified connection's platform, status and workspace —
        so naming another connection cannot cross a tenant boundary.
        """
        events: list[NormalizedEvent] = []
        for value in _changes(security.json_payload(request)):
            owner = self._change_connection(value, connection)
            if owner is None:
                continue
            names = _profile_names(value)
            events.extend(self._message_events(owner, value, names))
            events.extend(self._status_events(owner, value))
        return events

    def _change_connection(self, value: dict[str, Any], verified: ChannelConnection) -> ChannelConnection | None:
        """The connection one change belongs to, preferring the verified one.

        The common case — one delivery, one number — costs no query: the id in
        the payload is the verified connection's own ``external_id``.
        """
        phone_number_id = _phone_number_id(value)
        if not phone_number_id or phone_number_id == verified.external_id:
            return verified
        owner = self._connection_for(phone_number_id)
        if owner is None:
            logger.info("WhatsApp: a delivery named a phone number id this deployment has not connected.")
        return owner

    def _message_events(
        self,
        connection: ChannelConnection,
        value: dict[str, Any],
        names: dict[str, str],
    ) -> list[NormalizedEvent]:
        messages = value.get("messages")
        if not isinstance(messages, list):
            return []
        events: list[NormalizedEvent] = []
        for message in messages:
            if isinstance(message, dict):
                event = self._message_event(connection, message, names)
                if event is not None:
                    events.append(event)
        return events

    def _message_event(
        self,
        connection: ChannelConnection,
        message: dict[str, Any],
        names: dict[str, str],
    ) -> NormalizedEvent | None:
        sender = _wa_identity(message.get("from"))
        message_id = _text(message.get("id"), MAX_EXTRA_CHARS)
        if not sender or not message_id:
            # Every genuine message has both, and the id is the deduplication
            # key (SPEC §7.1 step 2). Without it a redelivery would be processed
            # again; without the sender there is no one this belongs to.
            return None

        timestamp = _timestamp(message.get("timestamp"))
        extra: dict[str, Any] = {}
        name = names.get(sender)
        if name:
            extra["profile_name"] = name

        button_id, title = _interactive_reply(message)
        if button_id:
            return NormalizedEvent(
                type=EventType.POSTBACK,
                connection=connection,
                platform_user_id=sender,
                provider_event_id=f"wa:{message_id}",
                timestamp=timestamp,
                payload=EventPayload(
                    button_id=_button_id(button_id),
                    text=title,
                    extra={**extra, "reply_id": button_id},
                ),
                raw=message,
            )

        # Bound once: the media id and the caption are read out of the key this
        # names, so three copies of the expression would be three chances for a
        # later edit to make them disagree.
        raw_type = _text(message.get("type"), 50)
        kind = _MEDIA_TYPES.get(raw_type)
        media_ids: tuple[str, ...] = ()
        text = _text(_message_text(message))
        if kind is not None:
            media_id = _media_id(message, raw_type)
            if media_id:
                media_ids = (media_id,)
                extra["media_kind"] = kind
                text = text or _media_caption(message, raw_type)

        if not text and not media_ids:
            # location and contacts have no text of their own, and SPEC §7.2 has
            # no field for either, so they arrive as the sentence a person would
            # have typed. Anything still empty after this is a type we do not
            # carry — a reaction, an order, a system notice — and is dropped.
            text = _location_text(message.get("location")) or _contact_text(message.get("contacts"))
            if not text:
                return None

        return NormalizedEvent(
            type=EventType.MESSAGE,
            connection=connection,
            platform_user_id=sender,
            provider_event_id=f"wa:{message_id}",
            timestamp=timestamp,
            payload=EventPayload(text=text, media_ids=media_ids, extra=extra),
            raw=message,
        )

    def _status_events(self, connection: ChannelConnection, value: dict[str, Any]) -> list[NormalizedEvent]:
        """``statuses[]`` to ``delivery_status`` events (SPEC §6.5).

        The payload shape is exactly what ``apps.messaging.ingest`` already
        consumes — ``provider_message_id``, ``status``, ``error`` in ``extra`` —
        so nothing about WhatsApp reaches that module. The ladder rules, the
        out-of-order handling and the compare-and-set all belong there.
        """
        statuses = value.get("statuses")
        if not isinstance(statuses, list):
            return []

        events: list[NormalizedEvent] = []
        for status in statuses:
            if not isinstance(status, dict):
                continue
            message_id = _text(status.get("id"), MAX_EXTRA_CHARS)
            state = _text(status.get("status"), 30)
            if not message_id or state not in _RECEIPT_STATUSES:
                continue
            extra: dict[str, Any] = {"provider_message_id": message_id, "status": state}
            if state == "failed":
                extra["error"] = _status_error(status)
            events.append(
                NormalizedEvent(
                    type=EventType.DELIVERY_STATUS,
                    connection=connection,
                    platform_user_id=_wa_identity(status.get("recipient_id")),
                    # Namespaced by the state, not just the message: one message
                    # produces sent, delivered and read, and a single id per
                    # message would file the second and third as duplicates of
                    # the first and lose them.
                    provider_event_id=f"wa:status:{state}:{message_id}",
                    timestamp=_timestamp(status.get("timestamp")),
                    payload=EventPayload(extra=extra),
                    raw=status,
                )
            )
        return events

    # -- outbound -----------------------------------------------------------

    def send(self, connection: ChannelConnection, identity: Any, outbound: OutboundMessage) -> SendResult:
        """Deliver one message, downgrading it first (SPEC §6.1).

        The downgrade can turn one abstract message into several — a gallery
        becomes one message per card — and they go in order. The result reports
        the **last** provider id: it is the message the contact is looking at,
        and the one a reply or a delivery receipt will reference.

        **A multi-part send is not atomic**, and cannot be here, for the reason
        ``telegram.TelegramAdapter.send`` sets out at length: SPEC §9.4 keys
        idempotency on the message *row*, of which there is one, so a retry
        after a partial failure re-sends the parts that already arrived. The
        behaviour is duplicate rather than drop, which is the right direction
        for a message a flow author intended to send, and it is written down
        rather than discovered in production.
        """
        to = str(getattr(identity, "platform_user_id", "") or "")
        if not to:
            return SendResult(status=SendStatus.FAILED, error="no_recipient")

        credentials = credentials_of(connection)
        token = credentials.get(ACCESS_TOKEN_KEY, "")
        phone_number_id = credentials.get(PHONE_NUMBER_ID_KEY, "") or connection.external_id

        payloads = self._payloads(to, outbound)
        if not payloads:
            # Nothing sendable survived. Reported rather than silently counted
            # as sent, so contract 1's message row says what happened.
            return SendResult(status=SendStatus.FAILED, error="empty_message")

        provider_message_id = ""
        for payload in payloads:
            body = call(token, f"{phone_number_id}/messages", payload)
            provider_message_id = _sent_message_id(body) or provider_message_id
        return SendResult(status=SendStatus.SENT, provider_message_id=provider_message_id)

    def _payloads(self, to: str, outbound: OutboundMessage) -> list[dict[str, Any]]:
        """Everything this send puts on the wire, in order.

        **A template send is exactly one payload, and does not go through the
        downgrade renderer at all.** The renderer's job is to fit blocks into
        what a platform can carry, and a template has no blocks to fit — Meta
        holds the approved copy and we send a name plus parameters. Worse than
        pointless: the renderer turns a gallery into one ``OutboundMessage`` per
        card and copies ``template_ref`` onto every one of them, so a node with a
        gallery *and* a template sent the same approved template once per card.
        The contact received it three times and the account was billed three
        times, because each is a separate conversation-initiating message.

        Session sends keep the renderer, which is what they are for.
        """
        if outbound.template_ref:
            return [_template_payload(to, outbound)]

        rendered = downgrade(outbound, self.capabilities)
        return [payload for message in rendered.messages for payload in wire_calls(to, message)]

    # `send_typing` and `mark_seen` stay the base class's no-ops. Both of
    # WhatsApp's equivalents are addressed to a specific inbound ``wamid``
    # rather than to a conversation, and SPEC §6.1's signature passes an
    # identity — there is no message id to mark. Inventing one by querying for
    # the last inbound message would put a database read on the typing path for
    # a cosmetic effect.

    # -- lifecycle ----------------------------------------------------------

    def on_disconnect(self, connection: ChannelConnection) -> None:
        """Unsubscribe the app, so a removed number stops delivering to us.

        **Only when no other connection still needs that subscription.** The
        Graph call is ``DELETE /<waba_id>/subscribed_apps``, and the subscription
        it removes belongs to the *WhatsApp Business Account*, not to the phone
        number being disconnected. A WABA routinely carries several numbers, so
        disconnecting one of them silently stopped every webhook for the others
        — an outage with no error, on connections nobody touched, that would
        look like Meta having gone quiet.

        The check spans workspaces deliberately. A subscription is deployment-
        wide state: two tenants can hold two numbers of the same WABA, and the
        second one's deliveries stop just as dead. ``credentials`` is encrypted
        and therefore cannot be filtered in SQL, so this reads the candidate
        rows and compares in Python — bounded by the number of WhatsApp
        connections a deployment has, on a path that runs once per disconnect.
        """
        credentials = credentials_of(connection)
        waba_id = credentials.get(WABA_ID_KEY, "")
        if not waba_id:
            return
        if _waba_still_in_use(waba_id, excluding=connection):
            logger.info(
                "WhatsApp: leaving the subscription on WhatsApp Business Account %s in place; "
                "another connected number still uses it.",
                waba_id,
            )
            return
        unsubscribe_app(credentials.get(ACCESS_TOKEN_KEY, ""), waba_id)


def _waba_still_in_use(waba_id: str, *, excluding: ChannelConnection) -> bool:
    """True when another connection is still attached to this WABA.

    Cross-tenant on purpose, and greppably so (CONTRIBUTING): the webhook
    subscription this guards is per WhatsApp Business Account, which is
    deployment-wide state rather than a workspace's.
    """
    others = (
        # Cross-tenant by necessity: a WABA subscription spans workspaces.
        ChannelConnection.objects.unscoped().filter(platform=Platform.WHATSAPP.value).exclude(pk=excluding.pk)
    )
    return any(credentials_of(other).get(WABA_ID_KEY, "") == waba_id for other in others)


def _sent_message_id(body: Any) -> str:
    """``messages[0].id`` from a send response, as the string the column holds.

    A response without one leaves the previous value standing rather than
    blanking it — several calls make up one logical send, and only the ones that
    produced a message have an id.
    """
    if not isinstance(body, dict):
        return ""
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages or not isinstance(messages[0], dict):
        return ""
    raw = messages[0].get("id")
    return str(raw)[:200] if isinstance(raw, str) else ""


def _message_text(message: dict[str, Any]) -> str:
    """The body of a text message, or "" for any other type."""
    text = message.get("text")
    return text.get("body", "") if isinstance(text, dict) and isinstance(text.get("body"), str) else ""


register_adapter(Platform.WHATSAPP, WhatsAppAdapter)
