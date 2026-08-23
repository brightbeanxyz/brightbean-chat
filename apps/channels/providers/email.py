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

__all__ = ["CONFIRM_SUBSCRIPTION_ACTION", "EmailAdapter", "SUPPRESSION_PROCESSOR", "compliance_headers", "compose"]

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

#: An SNS topic ARN gets its own, wider cap: ``arn:aws:sns:<region>:<account>:<name>``
#: with a name of up to 256 characters does not fit in the generic one.
MAX_TOPIC_ARN_CHARS = 512

#: How many recipients one notification may name before the rest are ignored.
#: A bounce for more than this is not a bounce, and the cap bounds the work an
#: unauthenticated delivery can ask for (SECURITY-BASELINE §2).
MAX_RECIPIENTS = 50


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
    for message in rendered.messages:
        html_parts.extend(_blocks_to_html(message))
    body_html = email_html.sanitize("\n".join(part for part in html_parts if part))
    body_html, body_text = email_html.with_unsubscribe_footer(
        body_html, email_html.to_plain_text(body_html).strip(), unsubscribe_link
    )

    from_address = _sender_address(connection, credentials, outbound.from_override)
    domain = from_address.partition("@")[2]
    return email_backends.Envelope(
        # Normalised, and by the same function the suppression gate used. Left
        # raw, the address that was checked against the suppression list and the
        # address actually mailed could be two different strings.
        to=normalize_email(str(getattr(identity, "platform_user_id", "") or "")),
        subject=outbound.subject[:MAX_SUBJECT_CHARS],
        html=body_html,
        text=body_text,
        from_address=from_address,
        from_name=str(credentials.get("from_name") or ""),
        headers=compliance_headers(unsubscribe_link),
        message_id=email_backends.new_message_id(domain),
    )


def _sender_address(connection: ChannelConnection, credentials: dict[str, Any], override: str) -> str:
    """The From address for one send: the override when it is allowed, else the connection's.

    **An override may only use the connection's own sending domain.** Composing
    a message is reached from the ``send_email`` node, whose config any holder of
    ``edit_flows`` may write — and ``manage_channels``, the permission that
    decides what this channel sends *as*, is admin-only
    (``apps.members.roles._ADMIN_ONLY_KEYS``). Without this check an Editor
    could pick any From address at all, which is both a spoofing primitive on a
    permissive relay and a way around ``external_id`` being deployment-unique
    precisely so one domain has one owner.

    Same-domain overrides are the case SPEC §11.10 exists for — ``billing@``
    rather than ``hello@`` — and they stay allowed.
    """
    configured = normalize_email(str(credentials.get("from_address") or ""))
    wanted = normalize_email(override)
    if not wanted:
        return configured
    if wanted.partition("@")[2] == str(connection.external_id or "").strip().lower():
        return wanted
    logger.warning(
        "Connection %s: refused a from-override outside the sending domain; using the connection's address.",
        connection.pk,
    )
    return configured


def compliance_headers(unsubscribe_link: str) -> dict[str, str]:
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
        addresses = _recipients(data.get("to"))
        provider_message_id = _text(data.get("email_id"))
        occurred_at = _timestamp(payload.get("created_at"))
        event_id = _text(payload.get("id")) or channels_ingest.synthetic_event_id(payload, prefix="resend")

        if event_type == "email.delivered":
            return _delivery_only(connection, addresses, provider_message_id, event_id, occurred_at, "delivered")
        if event_type == "email.complained":
            return _bounce_events(
                connection,
                addresses,
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
                addresses,
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
        topic_arn = _text(payload.get("TopicArn"), limit=MAX_TOPIC_ARN_CHARS)
        if not _topic_is_ours(connection, topic_arn):
            return []
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
            addresses = _recipients((message.get("delivery") or {}).get("recipients"))
            return _delivery_only(connection, addresses, provider_message_id, event_id, occurred_at, "delivered")
        if notification == "Complaint":
            complaint = message.get("complaint")
            addresses = _sns_recipients(complaint, "complainedRecipients")
            return _bounce_events(
                connection,
                addresses,
                provider_message_id,
                event_id,
                occurred_at,
                hard=True,
                reason=SuppressionReason.COMPLAINT.value,
                detail=_text(complaint.get("complaintFeedbackType") if isinstance(complaint, dict) else ""),
            )
        if notification == "Bounce":
            bounce = message.get("bounce")
            addresses = _sns_recipients(bounce, "bouncedRecipients")
            bounce_type = _text(bounce.get("bounceType") if isinstance(bounce, dict) else "")
            subtype = _text(bounce.get("bounceSubType") if isinstance(bounce, dict) else "")
            return _bounce_events(
                connection,
                addresses,
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
        # Its own limit: an SNS topic name may be 256 characters, so a real ARN
        # routinely exceeds the generic 200-character field cap — and a
        # truncated one names no topic, leaving the subscription pending
        # forever with only a queue failure to show for it.
        topic_arn = _text(payload.get("TopicArn"), limit=MAX_TOPIC_ARN_CHARS)
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


def _topic_is_ours(connection: ChannelConnection, topic_arn: str) -> bool:
    """Whether this notification came from the topic this connection listens to.

    **A valid SNS signature proves AWS sent the payload, not that *your* topic
    did.** Anyone can create a topic in their own AWS account and publish a
    message whose body is a bounce notification naming somebody else's address;
    AWS signs it with a genuine certificate from ``sns.<region>.amazonaws.com``,
    so :func:`email_signatures.verify_sns` passes it. Without this check that
    payload would suppress arbitrary addresses in whichever workspace's
    connection id it was posted to.

    The expected ARN is stored on the connection. An operator can set it when
    connecting; otherwise it is recorded from the first subscription
    confirmation and enforced from then on, so the window in which anything is
    accepted is the one before the topic is wired up at all.
    """
    expected = str(email_backends.credentials_of(connection).get("topic_arn") or "").strip()
    if not expected:
        return True
    if topic_arn == expected:
        return True
    logger.warning("Connection %s: refused an SNS delivery from an unexpected topic.", connection.pk)
    return False


def _delivery_only(
    connection: ChannelConnection,
    addresses: list[str],
    provider_message_id: str,
    event_id: str,
    occurred_at: datetime,
    status: str,
) -> list[NormalizedEvent]:
    if not provider_message_id:
        return []
    return [
        NormalizedEvent(
            type=EventType.DELIVERY_STATUS,
            connection=connection,
            platform_user_id=address,
            # One log row per recipient, so a notification naming three
            # mailboxes is not deduplicated down to one.
            provider_event_id=event_id if index == 0 else f"{event_id}:{index}",
            timestamp=occurred_at,
            payload=EventPayload(extra={"provider_message_id": provider_message_id, "status": status}),
        )
        for index, address in enumerate(addresses)
    ]


def _bounce_events(
    connection: ChannelConnection,
    addresses: list[str],
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
    events: list[NormalizedEvent] = []
    for index, address in enumerate(addresses):
        # Every id is distinct: the delivery and opt-out halves are two rows in
        # the event log, and so is each recipient of a multi-recipient bounce.
        # Sharing one would make the later rows look like duplicates and be
        # dropped by the `(connection, provider_event_id)` constraint.
        suffix = "" if index == 0 else f":{index}"
        if provider_message_id:
            events.append(
                NormalizedEvent(
                    type=EventType.DELIVERY_STATUS,
                    connection=connection,
                    platform_user_id=address,
                    provider_event_id=f"{event_id}{suffix}",
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
                    provider_event_id=f"{event_id}{suffix}:optout",
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


def _recipients(value: Any) -> list[str]:
    """Every address in a provider's recipient list, normalised and deduplicated.

    All of them, not the first: one notification legitimately names several
    mailboxes — a send that fanned out, or a complaint feedback loop — and
    suppressing only the first leaves the rest being mailed and bouncing, which
    is the pattern that gets a sending domain blocklisted.
    """
    items = [value] if isinstance(value, str) else value if isinstance(value, list) else []
    found: list[str] = []
    for item in items[:MAX_RECIPIENTS]:
        address = normalize_email(item) if isinstance(item, str) else ""
        if address and address not in found:
            found.append(address)
    return found


def _sns_recipients(container: Any, key: str) -> list[str]:
    """SES nests recipients as ``[{"emailAddress": "…"}]``."""
    if not isinstance(container, dict):
        return []
    entries = container.get(key)
    if not isinstance(entries, list):
        return []
    found: list[str] = []
    for entry in entries[:MAX_RECIPIENTS]:
        address = normalize_email(str(entry.get("emailAddress") or "")) if isinstance(entry, dict) else ""
        if address and address not in found:
            found.append(address)
    return found


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
    topic_arn = str(payload.get("topic_arn") or "")
    client = email_backends.ses_client(connection, service="sns")
    client.confirm_subscription(TopicArn=topic_arn, Token=payload.get("token"))
    _remember_topic(connection, topic_arn)
    logger.info("Confirmed an SNS bounce-topic subscription for connection %s.", connection.pk)


def _remember_topic(connection: ChannelConnection, topic_arn: str) -> None:
    """Pin the confirmed topic on the connection, once.

    This is what turns :func:`_topic_is_ours` from permissive into enforcing. It
    only ever writes the first one: overwriting on a later confirmation would
    let anyone who can reach the webhook re-point the connection at their own
    topic, which is the check's whole purpose.
    """
    credentials = email_backends.credentials_of(connection)
    if not topic_arn or credentials.get("topic_arn"):
        return
    connection.credentials = {**credentials, "topic_arn": topic_arn}  # type: ignore[assignment]
    connection.save(update_fields=["credentials", "updated_at"])


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
