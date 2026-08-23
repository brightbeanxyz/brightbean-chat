"""SPEC §6.7 — email, outbound only, over SMTP, Resend or SES.

``providers/telegram.py`` is this module's template and says so in its own
docstring: replace the helpers, keep the class. What is inherited and therefore
absent here is the whole of ``providers/base.py`` — HTTP mechanics, timeout
policy, ``429`` → ``RateLimitError``, "never put a URL in an error message".

--------------------------------------------------------------------------
Three backends, one adapter
--------------------------------------------------------------------------

Email is the only platform in v1 whose connections do not all talk to the same
API, so the seam between "what an email is" and "how it leaves" is drawn
explicitly: :mod:`apps.channels.providers.email_backends` owns the second half
and nothing else constructs an SMTP connection, a Resend request or a boto3
client. It is deliberately as narrow as ``apps/media_library/storage.py`` keeps
boto3 — deferred imports inside the functions that need them, so a deployment
using one provider never loads the other two.

This module owns the first half: what goes in the message, and the two rules
about it that are not negotiable.

--------------------------------------------------------------------------
Every email carries an unsubscribe, and nothing can turn it off
--------------------------------------------------------------------------

SPEC §6.7 puts ``List-Unsubscribe`` and a hosted footer link on *every* email,
in core. Both are added by :meth:`EmailAdapter.send`, which is downstream of
every path that can produce one — the ``send_email`` node, a broadcast, an inbox
reply, the public API — so there is no configuration surface that could omit
them and no caller that has to remember. RFC 8058's ``List-Unsubscribe-Post``
goes with it, which is what makes the one-click button appear in Gmail.

--------------------------------------------------------------------------
Suppression is checked here because here is last
--------------------------------------------------------------------------

``identity.opted_out_at`` is the compliance engine's business and is checked
before this method is ever reached. The *address* suppression list is checked
here, immediately before the wire, because a bounce has to survive the contact
being deleted and re-imported — at which point there is no identity left holding
the opt-out (see ``EmailSuppression``'s docstring for why
``apps/contacts/imports.py`` guarantees that).

A hit does two things: refuses the send, and opts the identity out through the
messaging facade. So the *second* attempt to mail a re-imported suppressed
address is refused by the chokepoint rather than by this adapter, and everything
that reads compliance set-wise sees it too.

--------------------------------------------------------------------------
Inbound is bounces, not messages
--------------------------------------------------------------------------

``Capabilities.inbound`` is ``False``: ``/webhooks/email/<provider>/<id>/``
carries delivery notifications, so :meth:`parse_events` emits
``DELIVERY_STATUS`` and ``OPT_OUT`` and never ``MESSAGE``. A hard bounce or a
complaint is an opt-out — permanently undeliverable and "please stop" are the
same instruction — while a soft bounce fails the message and nothing more.

--------------------------------------------------------------------------
Secrets
--------------------------------------------------------------------------

An SMTP password, a Resend key and an SES key pair all live encrypted in
``connection.credentials``. Nothing here logs one, and error messages carry a
code rather than a provider's prose, which routinely quotes the request that
produced it (SECURITY-BASELINE §5). ``apps/common/logging.py`` carries the
shapes of all three, and of the ``/u/`` token, as a backstop.
"""

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from django.utils import timezone

from apps.channels import ingest as channels_ingest
from apps.channels import security, suppression
from apps.channels.capabilities import Capabilities, capabilities_for
from apps.channels.downgrade import downgrade
from apps.channels.events import (
    Card,
    CardBlock,
    EventPayload,
    EventType,
    GalleryBlock,
    MediaBlock,
    NormalizedEvent,
    OutboundMessage,
    SendResult,
    SendStatus,
    TextBlock,
)
from apps.channels.models import ChannelConnection, SuppressionReason
from apps.channels.providers import email_backends, email_html, email_signatures
from apps.channels.providers.base import Adapter
from apps.channels.registry import register_adapter
from apps.channels.unsubscribe import unsubscribe_url
from apps.common.addresses import normalize_email
from apps.common.platforms import Platform
from apps.common.validators import is_renderable_url
from apps.queueing.registry import register_handler, schedule

if TYPE_CHECKING:
    from django.http import HttpRequest

logger = logging.getLogger(__name__)

__all__ = ["CONFIRM_SUBSCRIPTION_ACTION", "EmailAdapter", "SUPPRESSION_PROCESSOR", "compose"]

_CAPABILITIES: Capabilities = capabilities_for(Platform.EMAIL)

#: Cap on a rendered subject, matching the node schema's own limit. A subject
#: longer than this is truncated rather than refused: every mail client
#: truncates it anyway, and a refused send over a long subject would be a worse
#: outcome than a short one.
MAX_SUBJECT_CHARS = 300

#: The queue action that confirms an SNS topic subscription. Deliberately not
#: done inline: SPEC §21 wants webhook ack p95 under 500 ms, and an AWS API call
#: inside the ack is exactly the thing that misses it.
CONFIRM_SUBSCRIPTION_ACTION = "email_confirm_sns_subscription"

#: The inbound processor that turns a bounce into a suppression row.
SUPPRESSION_PROCESSOR = "email_suppression"

#: SES bounce types. "Permanent" is the mailbox saying it will never accept
#: mail; everything else is a condition that may clear.
_SES_PERMANENT = "Permanent"

#: Resend bounce classifications. Resend reports a subtype rather than a
#: hard/soft flag on some events, so both spellings are recognised.
_RESEND_HARD_BOUNCE = frozenset({"hard", "hardbounce", "permanent", "suppressed"})

#: Cap on any string read out of a provider payload before it is stored or
#: logged (SECURITY-BASELINE §2).
_MAX_FIELD_CHARS = 200


# ---------------------------------------------------------------------------
# Composing the message
# ---------------------------------------------------------------------------


def compose(
    connection: ChannelConnection,
    identity: Any,
    outbound: OutboundMessage,
    *,
    unsubscribe_link: str,
) -> email_backends.Envelope:
    """The finished :class:`~.email_backends.Envelope` for one send.

    Pure: no HTTP, no database, no clock beyond the Message-ID's. That is what
    makes ``test_email_outbound.py`` a table rather than a mock forest, and it
    is the same property ``telegram.wire_calls`` was written for.

    The abstract message becomes HTML rather than the other way round, because
    the capability row already says what email carries: ``text``, ``image`` and
    ``url_buttons``.

    **Buttons are handled before this function runs**, and that is worth knowing
    before looking for the code that renders them. Email declares
    ``buttons=False`` — an email has no button widget and no way to receive a
    press — so ``apps.channels.downgrade`` inlines every button into the text as
    ``label: url`` (URL buttons) or a numbered option (postbacks) before the
    adapter sees the message. What turns the first of those back into something
    clickable is the linkifying of bare URLs in ``email_html.sanitize``, which is
    what ``url_buttons=True`` means for a channel whose buttons are hyperlinks.
    """
    credentials = email_backends.credentials_of(connection)
    rendered = downgrade(outbound, _CAPABILITIES)

    html_parts: list[str] = []
    text_parts: list[str] = []
    for message in rendered.messages:
        html_parts.extend(_blocks_to_html(message))
    body_html = email_html.sanitize("\n".join(part for part in html_parts if part))
    body_text = email_html.to_plain_text(body_html)
    text_parts.append(body_text)

    body_html, body_text = email_html.with_unsubscribe_footer(
        body_html, "\n".join(text_parts).strip(), unsubscribe_link
    )

    from_address = normalize_email(outbound.from_override) or str(credentials.get("from_address") or "")
    domain = from_address.partition("@")[2]
    return email_backends.Envelope(
        to=str(getattr(identity, "platform_user_id", "") or ""),
        subject=(outbound.subject or str(credentials.get("default_subject") or ""))[:MAX_SUBJECT_CHARS],
        html=body_html,
        text=body_text,
        from_address=from_address,
        from_name=str(credentials.get("from_name") or ""),
        headers=_compliance_headers(unsubscribe_link),
        message_id=email_backends.new_message_id(domain),
    )


def _compliance_headers(unsubscribe_link: str) -> dict[str, str]:
    """``List-Unsubscribe`` and its RFC 8058 partner. On every message.

    The pair, never one of them: ``List-Unsubscribe-Post`` without a
    ``https`` entry in ``List-Unsubscribe`` is ignored, and ``List-Unsubscribe``
    alone gets a "unsubscribe" link buried in the client's overflow menu instead
    of the one-click button beside the sender's name.
    """
    return {
        "List-Unsubscribe": f"<{unsubscribe_link}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }


def _blocks_to_html(message: OutboundMessage) -> list[str]:
    parts: list[str] = []
    for block in message.blocks:
        if isinstance(block, TextBlock):
            parts.append(_paragraphs(block.text))
        elif isinstance(block, MediaBlock):
            parts.append(_media_html(block))
        elif isinstance(block, CardBlock):
            parts.append(_card_html(block.card))
        elif isinstance(block, GalleryBlock):
            parts.extend(_card_html(card) for card in block.cards)
    return [part for part in parts if part]


def _paragraphs(text: str) -> str:
    """Author text, already HTML by the time it gets here.

    ``send_email`` renders ``html_body`` in ``mode="html"``, so the *values* are
    escaped and the *markup* is the author's — which is the asymmetry
    ``apps/flows/rendering.py``'s docstring spends a paragraph on. Wrapping it in
    a paragraph only when it carries no block markup of its own keeps a
    single-line body from arriving unwrapped without double-wrapping a real
    document.
    """
    stripped = text.strip()
    if not stripped:
        return ""
    if stripped.startswith("<"):
        return stripped
    return f"<p>{stripped}</p>"


def _media_html(block: MediaBlock) -> str:
    """An image becomes an ``<img>``; anything else becomes a link.

    Email's capability row is ``image=True`` and nothing else, so audio, video
    and files arrive as a link to the media library's signed delivery URL rather
    than as an attachment — attachments are what get a sending domain
    reputation-scored downward, and the URL is what the library already mints.
    """
    if not is_renderable_url(block.url):
        return ""
    caption = block.caption.strip()
    if block.kind == "image":
        alt = caption or "Image"
        image = f'<img src="{block.url}" alt="{alt}" />'
        return f"<p>{image}</p>" if not caption else f"<p>{image}<br />{caption}</p>"
    label = caption or "Download"
    return f'<p><a href="{block.url}">{label}</a></p>'


def _card_html(card: Card) -> str:
    parts: list[str] = []
    if card.image_url and is_renderable_url(card.image_url):
        parts.append(f'<img src="{card.image_url}" alt="" />')
    if card.title:
        parts.append(f"<h3>{card.title}</h3>")
    if card.subtitle:
        parts.append(f"<p>{card.subtitle}</p>")
    for button in card.buttons:
        if button.is_url and is_renderable_url(button.url):
            parts.append(f'<p><a href="{button.url}">{button.label}</a></p>')
    return f"<div>{''.join(parts)}</div>" if parts else ""


# ---------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------


class EmailAdapter(Adapter):
    """SPEC §6.7, implemented against SPEC §6.1's interface."""

    platform = Platform.EMAIL.value
    capabilities = _CAPABILITIES
    webhook_content = "json"

    # -- outbound -----------------------------------------------------------

    def send(self, connection: ChannelConnection, identity: Any, outbound: OutboundMessage) -> SendResult:
        """Compose one email and hand it to the connection's backend.

        Returns ``FAILED`` with a machine-readable code for anything this
        adapter can decide on its own; raises ``APIError``/``RateLimitError``
        upward for anything the provider decided, because
        ``apps.messaging.services._dispatch`` owns the retry policy for those.
        """
        address = normalize_email(str(getattr(identity, "platform_user_id", "") or ""))
        if not address:
            return SendResult(status=SendStatus.FAILED, error="no_address")

        if suppression.is_suppressed(connection.workspace_id, address):
            # Refuse, and teach the identity what the address already knows, so
            # the compliance chokepoint answers next time — see the module
            # docstring.
            self._heal_suppressed_identity(connection, identity)
            return SendResult(status=SendStatus.FAILED, error="opted_out")

        envelope = compose(connection, identity, outbound, unsubscribe_link=unsubscribe_url(identity))
        if not envelope.from_address:
            return SendResult(status=SendStatus.FAILED, error="no_from_address")
        if not envelope.subject:
            # Every mail client shows "(no subject)" and every spam filter
            # notices. Reported rather than sent, so the row says why.
            return SendResult(status=SendStatus.FAILED, error="no_subject")
        if not envelope.html and not envelope.text:
            return SendResult(status=SendStatus.FAILED, error="empty_message")

        provider_message_id = email_backends.deliver(connection, envelope)
        return SendResult(status=SendStatus.SENT, provider_message_id=provider_message_id)

    def _heal_suppressed_identity(self, connection: ChannelConnection, identity: Any) -> None:
        """Record the opt-out this identity is missing. Never raises.

        The address is suppressed but this identity does not know it — which is
        the exact state a delete-and-re-import produces. Recording it here is
        what moves enforcement back to the compliance engine for every later
        send, and it is a best-effort repair: the send is already refused
        whatever happens to it.
        """
        if getattr(identity, "opted_out_at", None) is not None:
            return
        try:
            suppression.suppress_and_opt_out(
                identity,
                reason=SuppressionReason.HARD_BOUNCE.value,
                detail="rediscovered",
                connection=connection,
            )
        except Exception:
            logger.exception("Email: could not record an opt-out for a suppressed address on %s.", connection.pk)

    # -- inbound ------------------------------------------------------------

    def verify_webhook(self, request: "HttpRequest", connection: ChannelConnection) -> bool:
        """Whether this delivery really came from the connection's provider.

        Dispatched on the connection's stored provider rather than on the
        ``<provider>`` URL segment: the segment is not a credential and is not
        used for lookup (``views_webhooks.email_webhook`` says so), so trusting
        it would let a caller pick which verifier ran.

        SMTP answers ``False`` unconditionally. There is no SMTP callback, so a
        delivery on that route is either a misconfiguration or a probe, and both
        get the same 403 an unknown connection gets — which is the property
        ``tests/idor.py``'s waiver for this route depends on.
        """
        provider = email_backends.provider_for(connection)
        if provider == "resend":
            secret = str(email_backends.credentials_of(connection).get("signing_secret") or "")
            return email_signatures.verify_resend(request, request.body, secret)
        if provider == "ses":
            return email_signatures.verify_sns(security.json_payload(request) or {})
        return False

    def parse_events(self, request: "HttpRequest", connection: ChannelConnection) -> list[NormalizedEvent]:
        """One notification becomes zero, one or two normalized events.

        Two, for a hard bounce: the message failed *and* the address opted out,
        which are separate facts the pipeline applies in separate places
        (``_apply_delivery_status`` and ``apply_opt_out``).

        Defensive by contract (SECURITY-BASELINE §2). Everything here was
        written by a stranger until ``verify_webhook`` said otherwise, and even
        then the provider echoes attacker-supplied addresses back to us.
        """
        payload = security.json_payload(request) or {}
        if email_backends.provider_for(connection) == "resend":
            return self._from_resend(connection, payload)
        return self._from_sns(connection, payload)

    # -- Resend -------------------------------------------------------------

    def _from_resend(self, connection: ChannelConnection, payload: dict[str, Any]) -> list[NormalizedEvent]:
        event_type = _text(payload.get("type"))
        data = payload.get("data")
        if not isinstance(data, dict):
            return []
        address = _first_recipient(data.get("to"))
        provider_message_id = _text(data.get("email_id"))
        occurred_at = _timestamp(payload.get("created_at"))
        event_id = _text(payload.get("id")) or channels_ingest.synthetic_event_id(payload, prefix="resend")

        if event_type == "email.delivered":
            return _delivery_only(connection, address, provider_message_id, event_id, occurred_at, "delivered")
        if event_type == "email.complained":
            return _bounce_events(
                connection,
                address,
                provider_message_id,
                event_id,
                occurred_at,
                hard=True,
                reason=SuppressionReason.COMPLAINT.value,
                detail="complaint",
            )
        if event_type == "email.bounced":
            bounce = data.get("bounce")
            subtype = _text(bounce.get("type") if isinstance(bounce, dict) else "").lower()
            return _bounce_events(
                connection,
                address,
                provider_message_id,
                event_id,
                occurred_at,
                hard=subtype in _RESEND_HARD_BOUNCE,
                reason=SuppressionReason.HARD_BOUNCE.value,
                detail=subtype or "bounce",
            )
        # email.sent, email.delivery_delayed, email.opened, email.clicked and
        # anything Resend adds later. Open and click tracking is #26 (L7-A);
        # ignoring them here is what keeps that issue's routes the only place
        # they are counted.
        return []

    # -- SES via SNS --------------------------------------------------------

    def _from_sns(self, connection: ChannelConnection, payload: dict[str, Any]) -> list[NormalizedEvent]:
        envelope_type = _text(payload.get("Type"))
        if envelope_type == "SubscriptionConfirmation":
            self._schedule_subscription_confirmation(connection, payload)
            return []
        if envelope_type != "Notification":
            return []

        message = security.parse_json_body(_text(payload.get("Message"), limit=256_000).encode("utf-8"))
        if not isinstance(message, dict):
            return []

        notification = _text(message.get("notificationType")) or _text(message.get("eventType"))
        mail = message.get("mail")
        provider_message_id = _text(mail.get("messageId") if isinstance(mail, dict) else "")
        event_id = _text(payload.get("MessageId")) or channels_ingest.synthetic_event_id(payload, prefix="sns")
        occurred_at = _timestamp(payload.get("Timestamp"))

        if notification == "Delivery":
            address = _first_recipient((message.get("delivery") or {}).get("recipients"))
            return _delivery_only(connection, address, provider_message_id, event_id, occurred_at, "delivered")
        if notification == "Complaint":
            complaint = message.get("complaint")
            address = _sns_recipient(complaint, "complainedRecipients")
            return _bounce_events(
                connection,
                address,
                provider_message_id,
                event_id,
                occurred_at,
                hard=True,
                reason=SuppressionReason.COMPLAINT.value,
                detail=_text(complaint.get("complaintFeedbackType") if isinstance(complaint, dict) else ""),
            )
        if notification == "Bounce":
            bounce = message.get("bounce")
            address = _sns_recipient(bounce, "bouncedRecipients")
            bounce_type = _text(bounce.get("bounceType") if isinstance(bounce, dict) else "")
            subtype = _text(bounce.get("bounceSubType") if isinstance(bounce, dict) else "")
            return _bounce_events(
                connection,
                address,
                provider_message_id,
                event_id,
                occurred_at,
                hard=bounce_type == _SES_PERMANENT,
                reason=SuppressionReason.HARD_BOUNCE.value,
                detail=f"{bounce_type}/{subtype}".strip("/"),
            )
        return []

    def _schedule_subscription_confirmation(self, connection: ChannelConnection, payload: dict[str, Any]) -> None:
        """Arrange to confirm the topic subscription, out of band.

        **``SubscribeURL`` is deliberately never fetched.** It is a second
        attacker-supplied URL, and the same operation is available as
        ``sns:ConfirmSubscription`` against the AWS API with the credentials the
        connection already holds — so the confirmation costs no SSRF surface at
        all. The IAM permission that needs is documented in
        ``docs/channels/email.md``.

        Queued rather than done inline because SPEC §21 asks for a webhook ack
        p95 under 500 ms and an AWS round trip inside the ack does not fit.
        """
        topic_arn = _text(payload.get("TopicArn"))
        token = _text(payload.get("Token"), limit=2000)
        if not topic_arn or not token:
            return
        try:
            schedule(
                CONFIRM_SUBSCRIPTION_ACTION,
                timezone.now(),
                {"connection_id": str(connection.pk), "topic_arn": topic_arn, "token": token},
                workspace=connection.workspace,
                idempotency_key=f"{CONFIRM_SUBSCRIPTION_ACTION}:{connection.pk}:{topic_arn}",
            )
        except Exception:
            logger.exception("Email: could not enqueue an SNS subscription confirmation on %s.", connection.pk)


# ---------------------------------------------------------------------------
# Event construction
# ---------------------------------------------------------------------------


def _delivery_only(
    connection: ChannelConnection,
    address: str,
    provider_message_id: str,
    event_id: str,
    occurred_at: datetime,
    status: str,
) -> list[NormalizedEvent]:
    if not address or not provider_message_id:
        return []
    return [
        NormalizedEvent(
            type=EventType.DELIVERY_STATUS,
            connection=connection,
            platform_user_id=address,
            provider_event_id=event_id,
            timestamp=occurred_at,
            payload=EventPayload(extra={"provider_message_id": provider_message_id, "status": status}),
        )
    ]


def _bounce_events(
    connection: ChannelConnection,
    address: str,
    provider_message_id: str,
    event_id: str,
    occurred_at: datetime,
    *,
    hard: bool,
    reason: str,
    detail: str,
) -> list[NormalizedEvent]:
    """The message failed, and — for a hard bounce or a complaint — the address opted out.

    Two events rather than one, because the pipeline applies them in two
    different places and a soft bounce needs only the first. The opt-out is an
    ``EventType.OPT_OUT``, which is how ``apps.messaging.ingest`` sets
    ``opted_out_at`` — ROADMAP contract 3 reserves that column to itself, so an
    adapter that wrote it directly would fail the build's AST scan.
    """
    if not address:
        return []
    events: list[NormalizedEvent] = []
    if provider_message_id:
        events.append(
            NormalizedEvent(
                type=EventType.DELIVERY_STATUS,
                connection=connection,
                platform_user_id=address,
                provider_event_id=event_id,
                timestamp=occurred_at,
                payload=EventPayload(
                    extra={
                        "provider_message_id": provider_message_id,
                        "status": "failed",
                        "error": detail[:_MAX_FIELD_CHARS],
                    }
                ),
            )
        )
    if hard:
        events.append(
            NormalizedEvent(
                type=EventType.OPT_OUT,
                connection=connection,
                platform_user_id=address,
                # A distinct id from the delivery half: they are two rows in the
                # event log, and sharing one would make the second look like a
                # duplicate of the first and be dropped.
                provider_event_id=f"{event_id}:optout",
                timestamp=occurred_at,
                payload=EventPayload(extra={"suppression_reason": reason, "detail": detail[:_MAX_FIELD_CHARS]}),
            )
        )
    return events


# ---------------------------------------------------------------------------
# Reading a provider's payload
# ---------------------------------------------------------------------------


def _text(value: Any, limit: int = _MAX_FIELD_CHARS) -> str:
    """A bounded string, or ``""`` for anything that is not one."""
    return security.scrub_nulls(value)[:limit] if isinstance(value, str) else ""


def _first_recipient(value: Any) -> str:
    """The first address in a provider's recipient list, normalised."""
    if isinstance(value, str):
        return normalize_email(value)
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str) and (address := normalize_email(item)):
                return address
    return ""


def _sns_recipient(container: Any, key: str) -> str:
    """SES nests recipients as ``[{"emailAddress": "…"}]``."""
    if not isinstance(container, dict):
        return ""
    entries = container.get(key)
    if not isinstance(entries, list):
        return ""
    for entry in entries:
        if isinstance(entry, dict) and (address := normalize_email(str(entry.get("emailAddress") or ""))):
            return address
    return ""


def _timestamp(value: Any) -> datetime:
    """A provider timestamp, or now. Never raises.

    ISO 8601 in both providers, with SES using ``Z`` where Python wants
    ``+00:00`` until 3.11. Falls back to now rather than dropping the event: the
    time an event happened is metadata, and losing a bounce over it would be the
    wrong trade.
    """
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return timezone.now()
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return timezone.now()


# ---------------------------------------------------------------------------
# Suppression, from the inbound seam
# ---------------------------------------------------------------------------


def record_suppressions(connection: ChannelConnection, events: Any) -> None:
    """Write a suppression row for every opt-out on an email connection.

    Registered on the contract-6 dispatch seam rather than done inside
    ``parse_events``, which is a parser and stays one. It runs at
    ``LATE_ORDER`` so ``apps.messaging.ingest`` has already created the identity
    and stamped ``opted_out_at`` — this adds the half that outlives the identity.

    Filtered to email connections: the seam is global, and every other platform's
    opt-out is fully expressed by ``opted_out_at`` because no other platform's
    address survives its identity.
    """
    if connection.platform != Platform.EMAIL.value:
        return
    for event in events:
        if event.type != EventType.OPT_OUT:
            continue
        extra = event.payload.extra if isinstance(event.payload.extra, dict) else {}
        suppression.suppress(
            connection.workspace,
            event.platform_user_id,
            reason=str(extra.get("suppression_reason") or SuppressionReason.HARD_BOUNCE.value),
            detail=str(extra.get("detail") or ""),
            connection=connection,
        )


# ---------------------------------------------------------------------------
# Queue handler
# ---------------------------------------------------------------------------


@register_handler(CONFIRM_SUBSCRIPTION_ACTION)
def confirm_sns_subscription(payload: dict[str, Any], action: Any) -> None:
    """Confirm an SNS topic subscription through the AWS API.

    Re-resolves the connection rather than trusting the payload's copy, which is
    the reasoning ``apps/flows/handlers.py`` gives for the same move: a scheduled
    row is a document that has been sitting in a table.
    """
    connection_id = payload.get("connection_id")
    if not connection_id:
        return
    # .unscoped() is deliberate: a queue handler for an inbound notification has
    # no session and therefore no workspace, and the action's own workspace is
    # the one that scheduled it.
    connection = (
        ChannelConnection.objects.unscoped()
        .filter(pk=connection_id, platform=Platform.EMAIL.value, workspace_id=action.workspace_id)
        .first()
    )
    if connection is None:
        logger.info("SNS confirmation for connection %s: it is gone.", connection_id)
        return
    client = email_backends.ses_client(connection, service="sns")
    client.confirm_subscription(TopicArn=payload.get("topic_arn"), Token=payload.get("token"))
    logger.info("Confirmed an SNS bounce-topic subscription for connection %s.", connection.pk)


register_adapter(Platform.EMAIL, EmailAdapter)

# Registered here rather than from ``ChannelsConfig.ready`` because this module
# is only imported by ``providers.load_adapters()``, which ready() calls — so
# import *is* the registration point, exactly as it is for the adapter above.
# ``register_processor`` replaces by name, so a re-import cannot stack two.
channels_ingest.register_processor(
    record_suppressions,
    name=SUPPRESSION_PROCESSOR,
    order=channels_ingest.LATE_ORDER,
)
