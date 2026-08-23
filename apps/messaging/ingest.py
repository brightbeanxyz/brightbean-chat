"""Inbound persistence — ROADMAP contract 6's second stage (SPEC §7.1 step 3).

``apps.channels`` verifies a delivery, dedups it against ``webhook_event_log``,
persists the raw payload and then hands each surviving :class:`NormalizedEvent`
to the processors registered on its seam. This module is the first of those
processors. It turns an event into rows:

    identity → contact → conversation → message, then the bookkeeping

and it is the **only** place ``identity.window_expires_at`` is ever assigned
(SPEC §8: "updated on every inbound event in the webhook path. Nowhere else.").
``apps/messaging/tests/test_write_sites.py`` asserts that by scanning the source
tree, because a second write site would be a messaging window that reopens
itself — invisible in review, and a compliance failure rather than a bug.

--------------------------------------------------------------------------
Why each event gets its own transaction
--------------------------------------------------------------------------

``process_events`` calls processors **outside** a transaction, and a Meta batch
carries several unrelated events for several unrelated people. One event that
cannot be persisted — a payload shape no adapter anticipated, a contact row that
lost a race — must not roll back its siblings, so the atomic block and the
``except`` are both per event. The seam already isolates *processors* from each
other; this isolates events from each other inside one.

--------------------------------------------------------------------------
Which event types produce what
--------------------------------------------------------------------------

=====================  ==========  ========  ======  ==================
EventType              Identity    Consent   Window  Message row
=====================  ==========  ========  ======  ==================
message, postback,     yes         yes       yes     yes
story_reply
story_mention,         yes         yes       yes     no
referral
follow                 yes         **no**    **no**  no
opt_out                yes         opts out  no      no
comment                **no**      no        no      no
comment, claimed       yes         yes       yes     no
message_deleted        no          no        no      no (redacts one)
delivery_status        no          no        no      no (updates one)
=====================  ==========  ========  ======  ==================

Four of those rows are decisions rather than transcription.

*A window opens only for an event the contact authored.* A story mention or an
m.me / ``?start=`` referral is the contact opening a conversation, so it opens
the window — SPEC §10's Ref URL and Welcome triggers depend on that, and a
welcome message that cannot be sent is a broken headline feature. A **follow**
is not: following a page is a relationship, not a message, and it carries no
consent to message back. Its identity is created with ``opt_in=False``, so
L5-A's follow trigger has something to match while compliance still refuses to
send.

*Activity events write no message row.* They are contact activity and a trigger
matches on them, but writing one into the thread would show an agent a
conversation the contact never had, and float an empty thread to the top of an
inbox sorted by ``last_message_at``.

*A comment writes nothing at all* — **unless it has been claimed.** A comment is
public, not a DM, and creating a contact per comment turns one viral post into a
contact-spam amplifier, so an ordinary comment event is ignored here. L4-A owns
the platform-agnostic comment infrastructure and reads the event off this same
seam.

The exception is the one case where the platform genuinely permits us to write
to the person: a comment that SPEC §10's once-per-comment guard has already
claimed, which an adapter re-dispatches carrying
:data:`PRIVATE_REPLY_CLAIMED_KEY`. That marker is why the amplifier stays shut —
a claim is once per comment and once per commenter per post, so the identity
count is bounded by the guard rather than by how viral the post went. Such an
event gets an identity, a consent record stamped ``OptInSource.COMMENT``, and
the messaging window, because the private reply is an ordinary outbound send and
has to pass the same compliance chokepoint as any other (SPEC §8). It still
writes **no** message row: their comment is not a DM they sent us.

*A deletion redacts rather than removes.* SPEC §6.3 and §19 both require
Instagram's ``message_deletions`` to "redact message body, keep row with status
deleted". The row keeps its place in the thread and its timestamps; the body is
replaced with a marker the inbox renders as a tombstone. It is matched on
``provider_message_id`` in **either** direction, because a contact can delete
their own message as easily as we can delete ours.

--------------------------------------------------------------------------
The clock is ours, not the platform's
--------------------------------------------------------------------------

``window_expires_at`` is computed from ``timezone.now()``, never from
``event.timestamp``. A platform timestamp is attacker-adjacent — it arrives
inside a signed-but-not-trusted payload — and letting it set the window means a
forged future timestamp buys an arbitrarily long right to send.

--------------------------------------------------------------------------
Delivery receipts and ``payload.extra``
--------------------------------------------------------------------------

``EventPayload`` has no field for "the provider message id this receipt refers
to", and widening it would mean editing another workstream's shipped app. So the
convention lives here, in the module that reads it, and Layer-5 adapters
populate ``payload.extra``:

    ``{"provider_message_id": "<id>", "status": "sent|delivered|read|failed",
    "error": "<machine-readable code, optional>"}``

Anything else is ignored rather than raised on: a receipt for a message this
deployment never sent is normal (a shared page, a restored backup), not an
error.

Two more keys travel the same way, for the same reason, and are read only here:

``provider_message_id`` on a **message** event
    The platform's own id for an *inbound* message. Optional — Telegram supplies
    none — and stored on the row when it is there, which is what lets a later
    ``message_deleted`` event find the message to redact. It also becomes a
    second deduplication line under the conditional unique constraint on
    ``(channel_connection, provider_message_id)``.

``private_reply_claimed`` on a **comment** event
    See the comment row of the table above.
"""

import logging
from datetime import timedelta
from typing import Any

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.channels import ingest as channels_ingest
from apps.channels.events import EventType, NormalizedEvent
from apps.channels.policy import policy_for
from apps.messaging.events import EVENT_MESSAGE_RECEIVED, emit
from apps.messaging.identities import bounded_address, bounded_key, record_consent, resolve_identity
from apps.messaging.models import (
    DELIVERY_PROGRESS,
    ContactChannelIdentity,
    Conversation,
    ConversationState,
    Message,
    MessageDirection,
    MessageStatus,
    OptInSource,
)

logger = logging.getLogger(__name__)

__all__ = [
    "PERSISTENCE_PROCESSOR",
    "ROUTING_PROCESSOR",
    "apply_opt_out",
    "persist_events",
    "register_processors",
]

#: The seam's stage names. Registration *replaces* under a name, so L4-A takes
#: the routing tail over by registering under :data:`ROUTING_PROCESSOR` — no
#: edit to this module, which is what contract 6 is for.
PERSISTENCE_PROCESSOR = "persistence"
ROUTING_PROCESSOR = "routing"

#: Events that become a row in the thread.
THREAD_EVENTS = frozenset({EventType.MESSAGE, EventType.POSTBACK, EventType.STORY_REPLY})

#: Contact-authored activity that is not thread content. Opens the window.
#:
#: ``comment`` is in here for the claimed case only — :func:`_persist_one`
#: refuses an unclaimed one before this set is consulted. See the module
#: docstring for why that exception exists and why it is bounded.
ACTIVITY_EVENTS = frozenset({EventType.STORY_MENTION, EventType.REFERRAL, EventType.COMMENT})

#: Creates an identity and nothing else — no consent, no window. See the table.
CONTACT_ONLY_EVENTS = frozenset({EventType.FOLLOW})

#: Every type that resolves an identity at all. A type outside this set either
#: updates an existing row (``delivery_status``, ``message_deleted``) or is
#: ignored.
IDENTITY_EVENTS = THREAD_EVENTS | ACTIVITY_EVENTS | CONTACT_ONLY_EVENTS | {EventType.OPT_OUT}

#: Where a claimed comment says so. Set by the adapter that took SPEC §10's
#: guard, never by a parser: at parse time nothing knows yet whether the comment
#: will be claimed, because the guard runs later, in the routing stage.
PRIVATE_REPLY_CLAIMED_KEY = "private_reply_claimed"

#: Where an adapter puts the platform's own id for a message. Used by a receipt
#: to name the message it refers to, and by an inbound message to record an id a
#: later deletion can find it by.
PROVIDER_MESSAGE_ID_KEY = "provider_message_id"

#: What replaces a deleted message's body. Self-describing on purpose: an
#: operator reading the row, or a GDPR export, should see that the content was
#: retracted rather than that it was never there. ``blocks`` stays present and
#: empty so every reader of the SPEC §7.2 body shape keeps working unchanged.
REDACTED_BODY: dict[str, Any] = {
    "blocks": [],
    "buttons": [],
    "quick_replies": [],
    "tag": None,
    "template_ref": None,
    "deleted": True,
}

#: What a ``delivery_status`` receipt may say. Narrower than
#: ``MessageStatus.values`` on purpose: ``queued`` is a state *we* put a message
#: into before calling anyone, never something a platform reports back, and
#: accepting it let a receipt walk a failed message backwards to queued and
#: clear its error — a row nothing would ever move again.
RECEIPT_STATUSES = frozenset({MessageStatus.SENT, MessageStatus.DELIVERED, MessageStatus.READ, MessageStatus.FAILED})

#: Room for the ``in:`` prefix inside ``Message.idempotency_key``'s 255.
MAX_EVENT_ID_CHARS = 200

#: Cap on stored inbound text (SECURITY-BASELINE §7). Generous — the widest
#: platform ceiling is email's 100k — and the point is that it is bounded at all,
#: because ``body`` is jsonb and an unbounded string in it is an unbounded row.
MAX_TEXT_CHARS = 100_000
MAX_ATTACHMENTS = 20
MAX_ATTACHMENT_URL_CHARS = 2000


def register_processors() -> None:
    """Register persistence, then the routing tail. Called from ``ready()``.

    Order is dispatch order, and it matters: routing has to see what persistence
    wrote. Registering the no-op here rather than leaving the slot empty means
    L4-A's arrival changes one registration instead of introducing an ordering
    question — and because re-registering a name *replaces in place* rather than
    appending, L4-A's real router inherits this slot, after persistence.

    The guard is not decoration. ``ready()`` runs in ``INSTALLED_APPS`` order,
    so if L4-A's app is listed before this one its real router is already
    registered by the time we get here, and an unguarded call would quietly
    replace it with a no-op — routing would stop, with nothing raising anywhere.
    """
    channels_ingest.register_processor(persist_events, name=PERSISTENCE_PROCESSOR)
    if ROUTING_PROCESSOR not in channels_ingest.registered_processors():
        channels_ingest.register_processor(route_events, name=ROUTING_PROCESSOR)


def route_events(connection: Any, events: Any) -> None:
    """The routing tail, as a no-op (contract 6).

    L4-A (#11) replaces this with the ordered hook registry —
    ``hard_optout → post_persist → resume → trigger → default_reply`` — by
    registering its own callable under :data:`ROUTING_PROCESSOR`. Nothing here
    or in ``apps.channels`` changes when it does.
    """


def persist_events(connection: Any, events: Any) -> None:
    """The contract-6 processor. One transaction and one ``except`` per event.

    Deliberately takes **no contact advisory lock.** SPEC §9.6's one-step-per-
    contact invariant is about advancing a state machine; this appends rows.
    Every write here is either an insert the database arbitrates through a unique
    constraint or a bookkeeping column written from our own monotonic-enough
    clock, so there is no read-modify-write to serialise — and a *blocking* lock
    would spend SPEC §7.1's 1.5 s inline budget waiting on whatever worker
    happens to hold it. L4-A's routing stage takes the lock, in its own
    transaction, which is the only place it can be taken anyway: a
    transaction-scoped lock cannot span two processors.
    """
    for event in events:
        if not _belongs_to(connection, event):
            continue
        try:
            with transaction.atomic():
                _persist_one(connection, event)
        except Exception:
            # Broad on purpose, and logged rather than raised: the seam turns a
            # raising processor into a failed *batch*, and one unparseable event
            # should not cost the five good ones delivered beside it.
            #
            # Nothing platform-supplied reaches the log line. The scrubber in
            # apps.common.logging handles credentials, not newlines, and an
            # attacker-controlled id in a log message is a log-injection
            # primitive. The event type is ours and the connection id is a UUID.
            logger.exception(
                "Inbound persistence failed for a %s event on connection %s",
                getattr(event, "type", "?"),
                connection.pk,
            )


def _belongs_to(connection: Any, event: NormalizedEvent) -> bool:
    """Refuse an event claiming a connection other than the verified one.

    ``views_webhooks`` groups a batch by each event's own connection and hands
    each group to the processors with the connection it authenticated, so a
    mismatch here is an upstream bug rather than a reachable attack. It is a
    cheap tenancy backstop on the one path where a wrong answer writes another
    workspace's data.
    """
    own = getattr(event, "connection", None)
    if own is not None and own.pk != connection.pk:
        logger.warning("Dropped an inbound event naming another connection on a %s delivery", connection.pk)
        return False
    return True


def _persist_one(connection: Any, event: NormalizedEvent) -> None:
    if event.type == EventType.DELIVERY_STATUS:
        _apply_delivery_status(connection, event)
        return
    if event.type == EventType.MESSAGE_DELETED:
        _apply_deletion(connection, event)
        return
    if event.type == EventType.COMMENT and not _claims_private_reply(event):
        # A comment nobody claimed. Public, not a DM, and no contact — see the
        # module docstring on the amplifier this refusal prevents.
        logger.debug("Ignoring an unclaimed comment on connection %s", connection.pk)
        return
    if event.type not in IDENTITY_EVENTS:
        logger.debug("Ignoring inbound event type %s on connection %s", event.type, connection.pk)
        return

    address = bounded_address(event.platform_user_id)
    if not address:
        # An empty id would collide with every other empty one — the same bug
        # views_webhooks._dedup_id fixed for provider event ids.
        logger.debug("Inbound %s event carries no usable address on connection %s", event.type, connection.pk)
        return

    now = timezone.now()
    resolution = resolve_identity(connection, address, occurred_at=now)
    identity = resolution.identity
    contact = resolution.contact

    if event.type == EventType.OPT_OUT:
        apply_opt_out(identity, now)
        return
    if event.type in CONTACT_ONLY_EVENTS:
        # A follow is a relationship, not a message: an identity to match on,
        # and no consent and no window to send through. See the table above.
        return

    conversation = _conversation_for(contact, connection)
    message = _insert_inbound(conversation, event) if event.type in THREAD_EVENTS else None
    if event.type in THREAD_EVENTS and message is None:
        # Already persisted. Returning before the bookkeeping is what stops a
        # redelivery re-extending the messaging window past first-receipt plus
        # window_hours.
        #
        # This guard covers thread events only, because the message row is what
        # it is built on. An activity event carries no row and so has no
        # second-line dedup: redelivery of one is caught upstream by
        # ``webhook_event_log``'s unique (connection, provider_event_id), and a
        # deliberate re-dispatch of the same batch re-extends its window by the
        # gap between the two calls. That is bounded by how long a caller takes
        # to retry rather than by anything a platform controls, which is why it
        # is documented here rather than defended against with a second table.
        return

    _record_activity(
        identity,
        contact,
        conversation,
        now,
        message_at=now if message else None,
        opt_in_source=OptInSource.COMMENT if event.type == EventType.COMMENT else OptInSource.MESSAGE_IN,
    )

    if message is not None:
        emit(
            EVENT_MESSAGE_RECEIVED,
            workspace_id=contact.workspace_id,
            contact_id=contact.pk,
            conversation_id=conversation.pk,
            message_id=message.pk,
            connection_id=connection.pk,
            platform=connection.platform,
        )


def _conversation_for(contact: Any, connection: Any) -> Conversation:
    """The thread for this contact on this connection, creating it if new.

    A thread marked ``done`` reopens. The inbox list filters on ``state`` (SPEC
    §14), so leaving it closed would file a message the contact just sent
    somewhere no agent is looking — and ``services.open_conversation`` already
    reopens on the outbound side, which made the closed half of the pair the
    odd one out rather than a deliberate choice.
    """
    conversation = (
        Conversation.objects.for_workspace(contact.workspace_id)
        .filter(contact=contact, channel_connection=connection)
        .first()
    )
    if conversation is not None:
        if conversation.state == ConversationState.DONE:
            conversation.state = ConversationState.OPEN
            conversation.save(update_fields=["state", "updated_at"])
        return conversation
    conversation = Conversation(contact=contact, channel_connection=connection)
    try:
        with transaction.atomic():
            conversation.save()
    except IntegrityError:
        # Lost the unique race to a concurrent delivery; its row is the thread.
        return (
            Conversation.objects.for_workspace(contact.workspace_id)
            .filter(contact=contact, channel_connection=connection)
            .get()
        )
    return conversation


def inbound_idempotency_key(event: NormalizedEvent) -> str:
    """The key that makes re-processing one event produce one row.

    ``webhook_event_log`` already dedups deliveries one layer up, so this is the
    second line rather than the first — and it is the line that holds when the
    seam is driven directly: a replayed batch, a retried processor, L4-A's
    ordered stages re-running after a partial failure. The uniqueness is the
    database's, not a prior read's, so two concurrent attempts cannot both win.

    The id is bounded by hashing rather than by slicing. Slicing to fit the
    column meant two ids agreeing on a long prefix produced one key, and the
    second — a genuinely different message — was discarded as a duplicate; a NUL
    in the id meanwhile made the insert fail outright. ``_dedup_id`` in the
    webhook view already scrubs and hashes for the log's own constraint, and
    this layer needs the same treatment for its own.
    """
    return f"in:{bounded_key(event.provider_event_id, limit=MAX_EVENT_ID_CHARS)}"


def _inbound_provider_message_id(event: NormalizedEvent) -> str:
    """The platform's own id for an inbound message, where an adapter sent one.

    Optional by design — Telegram supplies none — and stored when it is there for
    two reasons: a later ``message_deleted`` event names the message by it, and
    the conditional unique constraint on ``(channel_connection,
    provider_message_id)`` then dedups a redelivery a second way.

    Bounded by **hashing** rather than truncation, like every other identifier
    here: two ids agreeing on a long prefix would otherwise collide in that
    constraint and the second message would be refused as a duplicate of the
    first.
    """
    extra = event.payload.extra if isinstance(event.payload.extra, dict) else {}
    return bounded_key(_clean(extra.get(PROVIDER_MESSAGE_ID_KEY), 500), limit=MAX_EVENT_ID_CHARS)


def _insert_inbound(conversation: Conversation, event: NormalizedEvent) -> Message | None:
    """Insert the inbound row, or return None if this event is already stored."""
    message = Message(
        conversation=conversation,
        direction=MessageDirection.IN,
        body=_inbound_body(event),
        status=MessageStatus.DELIVERED,
        idempotency_key=inbound_idempotency_key(event),
        provider_message_id=_inbound_provider_message_id(event),
    )
    try:
        with transaction.atomic():
            message.save()
    except IntegrityError:
        logger.debug("Inbound event %s was already persisted; skipping.", event.provider_event_id)
        return None
    return message


def _inbound_body(event: NormalizedEvent) -> dict[str, Any]:
    """SPEC §7.2's body shape, from an untrusted payload.

    Mirrors ``OutboundMessage.to_body()`` so one renderer serves both directions.
    Everything here is attacker-controlled: it is stored **as delivered** (minus
    NUL, which Postgres cannot hold in a text field, and length caps), and it is
    escaped at render. Nothing in this app ever marks it safe.
    """
    payload = event.payload
    blocks: list[dict[str, Any]] = []
    text = _clean(payload.text, MAX_TEXT_CHARS)
    if text:
        blocks.append({"type": "text", "text": text})
    # Type-checked before slicing, not assumed. EventPayload's contract is that
    # an adapter meeting a wrongly typed key leaves the field at its default, and
    # ``text`` is guarded by _clean for exactly that reason — but an int here
    # used to raise TypeError, which persist_events swallows, so one bad field
    # cost the whole message rather than just its attachments.
    attachments = payload.attachments if isinstance(payload.attachments, list | tuple) else ()
    for url in attachments[:MAX_ATTACHMENTS]:
        cleaned = _clean(url, MAX_ATTACHMENT_URL_CHARS)
        if cleaned:
            # Recorded, never fetched: SECURITY-BASELINE §6 forbids a
            # server-side fetch of a platform-supplied URL until #15's guard.
            blocks.append({"type": "file", "url": cleaned, "caption": ""})
    body: dict[str, Any] = {
        "blocks": blocks,
        "buttons": [],
        "quick_replies": [],
        "tag": None,
        "template_ref": None,
    }
    if payload.button_id:
        # What the contact pressed. L3-B's resume matches on it.
        body["button_id"] = _clean(payload.button_id, 200)
    if payload.ref:
        body["ref"] = _clean(payload.ref, 2000)
    return body


def _clean(value: Any, limit: int) -> str:
    """A storable string: text only, NUL-free, bounded."""
    if not isinstance(value, str):
        return ""
    return value.replace("\x00", "")[:limit]


def _record_activity(
    identity: ContactChannelIdentity,
    contact: Any,
    conversation: Conversation,
    now: Any,
    *,
    message_at: Any = None,
    opt_in_source: str = OptInSource.MESSAGE_IN,
) -> None:
    """Window bookkeeping and recency. **The one write site for the window.**

    ``opt_in_source`` is a parameter rather than a constant because SPEC §11.8's
    audit has to say *how* consent was obtained, and "they sent us a message" and
    "they commented on our post" are different answers to that question.
    """
    changed = record_consent(identity, source=opt_in_source, now=now)
    identity.last_inbound_at = now
    changed.append("last_inbound_at")

    window_hours = policy_for(identity.platform).window_hours
    if window_hours is not None:
        identity.window_expires_at = now + timedelta(hours=window_hours)
        changed.append("window_expires_at")
    identity.save(update_fields=[*changed, "updated_at"])

    contact.last_interaction_at = now
    contact.save(update_fields=["last_interaction_at", "updated_at"])

    if message_at is not None:
        conversation.last_message_at = message_at
        conversation.save(update_fields=["last_message_at", "updated_at"])


def apply_opt_out(identity: ContactChannelIdentity, now: Any = None) -> bool:
    """Record a hard opt-out. ``True`` when this call is the one that set it.

    Idempotent — the first refusal is the one that counts, and re-stamping the
    timestamp on a second STOP would keep moving the audit's answer to "when did
    they withdraw consent?" forward every time they repeated themselves.

    **The only assignment to ``opted_out_at`` in the project.** ROADMAP contract 3
    pins that column to this module and ``tests/test_write_sites.py`` asserts it
    over the AST rather than trusting the prose. An operator-initiated opt-out from
    the CRM (issue #13) therefore does not write the column itself: it arrives
    through :func:`apps.messaging.services.record_opt_out`, the facade door, which
    delegates here. One write site, two ways in — which is the shape SPEC §19
    wants, because opt-out enforcement living in exactly one place is what makes
    it unbypassable.
    """
    if identity.opted_out_at is not None:
        return False
    identity.opted_out_at = now or timezone.now()
    identity.opt_in = False
    identity.save(update_fields=["opted_out_at", "opt_in", "updated_at"])
    logger.info("Identity %s opted out on connection %s", identity.pk, identity.channel_connection_id)
    return True


def _apply_delivery_status(connection: Any, event: NormalizedEvent) -> None:
    """Map a provider receipt onto the message it refers to.

    Silently ignores a receipt naming a message this deployment did not send,
    and refuses to move a message backwards along the delivery ladder: platforms
    do not promise receipt ordering, and a late "sent" must not un-read a
    message the agent can see was read.
    """
    extra = event.payload.extra if isinstance(event.payload.extra, dict) else {}
    provider_id = _clean(extra.get("provider_message_id"), 200)
    status = extra.get("status")
    if not provider_id or status not in RECEIPT_STATUSES:
        logger.debug("Unusable delivery_status payload on connection %s; ignored.", connection.pk)
        return

    message = (
        Message.objects.for_workspace(connection.workspace_id)
        .filter(
            channel_connection=connection,
            provider_message_id=provider_id,
            # Outbound only. An inbound row carries no provider id today, so
            # this cannot match one — but a receipt is by definition about a
            # message *we* sent, and saying so keeps a future adapter that
            # records inbound provider ids from walking a received message up
            # the delivery ladder.
            direction=MessageDirection.OUT,
        )
        .first()
    )
    if message is None:
        logger.debug("delivery_status for unknown provider message id on connection %s", connection.pk)
        return

    new_status, error = _next_status(message.status, status, _clean(extra.get("error"), 200))
    if new_status is None:
        return

    # Compare-and-set on the status we read. Two receipts for one message can
    # arrive in the same batch or on two web workers, and without the predicate
    # the later UPDATE would clobber the earlier one whatever the ladder says.
    updated = (
        Message.objects.for_workspace(connection.workspace_id)
        .filter(pk=message.pk, status=message.status)
        .update(status=new_status, error=error, updated_at=timezone.now())
    )
    if not updated:
        logger.debug("A concurrent receipt already advanced message %s", message.pk)


def _claims_private_reply(event: NormalizedEvent) -> bool:
    """Has SPEC §10's comment guard already claimed this comment?

    ``is True`` rather than truthiness: the value arrives inside an
    attacker-adjacent payload, and a marker that any non-empty string could set
    would be one webhook field away from creating a contact per comment.
    """
    extra = event.payload.extra if isinstance(event.payload.extra, dict) else {}
    return extra.get(PRIVATE_REPLY_CLAIMED_KEY) is True


def _apply_deletion(connection: Any, event: NormalizedEvent) -> None:
    """Redact a message the platform says no longer exists (SPEC §6.3, §19).

    The row is kept and its body replaced, which is the whole point: the thread
    keeps its shape, the inbox shows a tombstone where the message was, and the
    content is gone from the database rather than merely hidden by a flag the
    next reader might not check.

    Matched in **either** direction — a contact deletes their own DM as readily
    as we delete ours — which is the one thing this cannot share with
    :func:`_apply_delivery_status`, whose whole subject is messages we sent.

    Idempotent and terminal: the update is narrowed to rows not already deleted,
    so a redelivery is a no-op, and nothing walks a deleted row back onto the
    delivery ladder because ``DELETED`` is not in :data:`DELIVERY_PROGRESS`.
    """
    extra = event.payload.extra if isinstance(event.payload.extra, dict) else {}
    provider_id = _clean(extra.get(PROVIDER_MESSAGE_ID_KEY), 200)
    if not provider_id:
        logger.debug("Unusable message_deleted payload on connection %s; ignored.", connection.pk)
        return

    updated = (
        Message.objects.for_workspace(connection.workspace_id)
        .filter(channel_connection=connection, provider_message_id=provider_id)
        .exclude(status=MessageStatus.DELETED)
        .update(body=dict(REDACTED_BODY), status=MessageStatus.DELETED, updated_at=timezone.now())
    )
    if not updated:
        # A message this deployment never stored, or one already redacted.
        # Normal rather than exceptional: an account can be connected after a
        # conversation started, and platforms redeliver.
        logger.debug("message_deleted named no storable message on connection %s", connection.pk)
        return
    logger.info("Redacted %s message(s) on connection %s at the platform's request.", updated, connection.pk)


def _next_status(current: str, incoming: str, error: str) -> tuple[str | None, str]:
    """The status to write and the error to write with it, or ``(None, "")``.

    Pure, so the whole 6x6 table is testable without a database. The rules:

    1. **The ladder only moves forward.** ``queued -> sent -> delivered -> read``.
       Platforms do not promise receipt ordering — Meta routinely sends them out
       of order — and a late "sent" must not un-read a message an agent can see
       was read.
    2. **``failed`` is only written over ``queued`` or ``sent``.** A failure
       receipt for a message the platform already reported delivered is a stale
       retransmission, and acting on it tells an operator that a delivered
       message failed.
    3. **A delivery receipt beats ``failed``.** Arriving is stronger evidence
       than a send-time error, and PR 2's retry path must not re-send something
       that actually landed.
    """
    if incoming == MessageStatus.FAILED:
        if DELIVERY_PROGRESS.get(current, 99) > DELIVERY_PROGRESS[MessageStatus.SENT]:
            return None, ""  # rule 2
        return (None, "") if current == MessageStatus.FAILED else (incoming, error or "provider_failed")

    if current == MessageStatus.FAILED:
        return incoming, ""  # rule 3 — and the failure code goes with it

    if DELIVERY_PROGRESS[incoming] <= DELIVERY_PROGRESS[current]:
        return None, ""  # rule 1
    return incoming, ""
