"""The messaging service facade — ROADMAP contract 1.

    L3-B's nodes and L4-D's inbox mutate messaging state **only** through this
    facade.

So the signatures here are load-bearing: two workstreams are written against
them without reading this file, and ``tests/test_facade_contract.py`` pins them
so a rename breaks in this tree rather than in theirs.

--------------------------------------------------------------------------
send_outbound, in the order SPEC §9.4 fixes
--------------------------------------------------------------------------

1. conversation for (contact, connection);
2. identity — missing means a ``failed`` row with ``no_identity``, not a raise;
3. :func:`~apps.messaging.compliance.can_send` — a denial is a ``failed`` row
   carrying a machine-readable code;
4. **insert the message row before the provider call**, and let an
   idempotency-key conflict mean "somebody else owns this send";
5. an agent send pauses automation;
6. a token from the connection's bucket;
7. the adapter;
8. record what happened.

**It never raises.** SPEC §9.5 says a failed send follows the ``default`` edge
onward rather than killing the flow, and a function that raised from the retry
handler but not inline would be two behaviours wearing one name. Every failure
comes back as a ``Message`` whose ``status`` and ``error`` say what went wrong.

--------------------------------------------------------------------------
Two guards, because the unique key alone is not enough
--------------------------------------------------------------------------

The unique index on ``(conversation, idempotency_key)`` stops a *second row*.
It does not by itself stop a second **provider call**: a thousand callers racing
on one key all block on the index, then all read back the same ``queued`` row,
and a naive implementation would have all of them send. So the row insert
answers "who owns this message" and a separate compare-and-set on
``dispatched_at`` answers "who owns this attempt". Only the winner of the second
one calls the adapter.

``dispatched_at`` doubles as SPEC §9.4's unknown-outcome signal: set with an
empty ``provider_message_id`` means the call went out and we never learned the
result, which is the only state where a re-send carries duplicate risk — and the
only state :mod:`apps.messaging.lookup` is consulted for.

--------------------------------------------------------------------------
What a caller has to have committed
--------------------------------------------------------------------------

Nothing here opens a transaction around the provider call, so at the top level
the message row is committed before the adapter is touched and a crash mid-call
leaves a ``queued`` row a retry can find. Called from inside an open transaction
— the worker's handler, or L3-B's runner under its contact lock — the nested
``atomic()`` is a savepoint rather than a commit, and that window widens to the
caller's transaction. That is the same window SPEC §9.4 already describes and
accepts; closing it entirely needs a second database connection, which is a
change to how this app talks to Postgres and is not one to make for a
hypothetical.
"""

import logging
from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F, Value
from django.db.models.functions import Greatest
from django.utils import timezone

from apps.channels.events import OutboundMessage, SendResult, SendStatus
from apps.channels.providers.exceptions import APIError, RateLimitError
from apps.channels.registry import adapter_for
from apps.messaging import buckets
from apps.messaging.codes import Denial, Failure
from apps.messaging.compliance import Allowed, can_send
from apps.messaging.models import (
    ContactChannelIdentity,
    Conversation,
    ConversationState,
    Message,
    MessageDirection,
    MessageSource,
    MessageStatus,
    OptInSource,
)

logger = logging.getLogger(__name__)

__all__ = [
    "AGENT_AUTOMATION_PAUSE",
    "assign_conversation",
    "close_conversation",
    "open_conversation",
    "pause_automation",
    "send_as_agent",
    "send_outbound",
    "send_via_api",
    "upsert_contact_identity",
]

#: SPEC §14: "Agent send sets conversation.automation_paused_until = now + 30
#: min (constant, ws-configurable later)."
AGENT_AUTOMATION_PAUSE = timedelta(minutes=30)

#: HTTP status codes that mean "try again", beyond the 5xx range. 408 is a
#: request timeout and 425 is "too early"; both are the platform saying the
#: request did not land, not that it was wrong.
_RETRYABLE_STATUSES = frozenset({408, 425})


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------


def open_conversation(*, workspace: Any, contact: Any, connection: Any) -> Conversation:
    """The thread for this contact on this connection, creating it if new.

    Also reopens a thread marked done: an outbound message on a closed
    conversation is an agent or an automation picking it back up, and leaving it
    ``done`` would hide the message from the list it belongs in.
    """
    conversation = (
        Conversation.objects.for_workspace(workspace).filter(contact=contact, channel_connection=connection).first()
    )
    if conversation is None:
        conversation = Conversation(contact=contact, channel_connection=connection)
        try:
            with transaction.atomic():
                conversation.save()
        except IntegrityError:
            conversation = (
                Conversation.objects.for_workspace(workspace)
                .filter(contact=contact, channel_connection=connection)
                .get()
            )
    if conversation.state == ConversationState.DONE:
        conversation.state = ConversationState.OPEN
        conversation.save(update_fields=["state", "updated_at"])
    return conversation


def close_conversation(conversation: Conversation) -> Conversation:
    """Mark a thread done (SPEC §14)."""
    conversation.state = ConversationState.DONE
    conversation.save(update_fields=["state", "updated_at"])
    return conversation


def assign_conversation(conversation: Conversation, assignee: Any) -> Conversation:
    """Assign a thread to a member, or to nobody when ``assignee`` is None."""
    conversation.assignee = assignee
    conversation.save(update_fields=["assignee", "updated_at"])
    return conversation


def pause_automation(conversation: Conversation, until: datetime | None) -> Conversation:
    """Set (or clear) the automation pause — **the only write site**.

    ROADMAP contract 3 makes ``automation_paused_until`` this app's to write and
    everybody else's to read, and ``apps/messaging/tests/test_write_sites.py``
    asserts that by scanning the source tree. The manual pause/resume toggle in
    L4-D's inbox is this function; so is the agent-send pause below.
    """
    conversation.automation_paused_until = until
    conversation.save(update_fields=["automation_paused_until", "updated_at"])
    return conversation


def _extend_automation_pause(conversation: Conversation, by: timedelta) -> Conversation:
    """Push the pause out by ``by``, never pulling it in.

    An operator who paused automation for two hours has said something more
    deliberate than an agent typing a reply; a reply that shortened it to thirty
    minutes would quietly undo an explicit instruction.
    """
    now = timezone.now()
    current = conversation.automation_paused_until
    target = max(now + by, current) if current is not None else now + by
    return pause_automation(conversation, target)


# ---------------------------------------------------------------------------
# Identities
# ---------------------------------------------------------------------------


def upsert_contact_identity(
    contact: Any,
    platform: str,
    address: str,
    *,
    source: str,
    opt_in: bool,
    connection: Any = None,
) -> ContactChannelIdentity:
    """Create or refresh an identity, recording the consent audit (SPEC §11.8).

    Connection resolution, per contract 1: one identity row per active
    connection of that platform; if none exists at capture time, a
    connection-less **pending** record is stored and upgraded lazily at first
    send. That upgrade happens in :func:`_identity_for` rather than here,
    because "first send" is when a connection is finally in hand.

    Consent only ever moves forward. ``opt_in_at`` is stamped once, when
    permission was first given — refreshing it on every touch would replace the
    moment consent was given with the moment it was last exercised, which is not
    the fact the audit is asking for — and an ``opted_out_at`` is never cleared
    from here. Withdrawing consent is a deliberate act (SPEC §19); re-granting
    it has to be one too, and this function is called by imports and APIs.
    """
    from apps.messaging.identities import bounded_address

    address = bounded_address(address)
    if not address:
        raise ValueError("An identity needs an address.")
    connection = connection or _default_connection(contact.workspace_id, platform)

    identity = _existing_identity(contact, platform, address, connection)
    if identity is None:
        identity = ContactChannelIdentity(
            contact=contact,
            channel_connection=connection,
            platform=platform,
            platform_user_id=address,
        )
    elif connection is not None and identity.channel_connection_id is None:
        identity.channel_connection = connection

    if opt_in and identity.opted_out_at is None:
        identity.opt_in = True
        identity.opt_in_at = identity.opt_in_at or timezone.now()
        identity.opt_in_source = identity.opt_in_source or source or OptInSource.MANUAL
    identity.save()
    return identity


def _existing_identity(contact: Any, platform: str, address: str, connection: Any) -> ContactChannelIdentity | None:
    """This contact's row for the address — the real one, or the pending one."""
    rows = ContactChannelIdentity.objects.for_workspace(contact.workspace_id).filter(
        contact=contact, platform=platform, platform_user_id=address
    )
    if connection is not None:
        matched = rows.filter(channel_connection=connection).first()
        if matched is not None:
            return matched
    return rows.filter(channel_connection__isnull=True).first()


def _default_connection(workspace_id: Any, platform: str) -> Any:
    """An active connection of ``platform``, or None for a pending record."""
    from apps.channels.models import ChannelConnection, ConnectionStatus

    return (
        ChannelConnection.objects.for_workspace(workspace_id)
        .filter(platform=platform, status=ConnectionStatus.ACTIVE)
        .order_by("created_at")
        .first()
    )


def _identity_for(workspace: Any, contact: Any, connection: Any) -> ContactChannelIdentity | None:
    """The identity to send to, upgrading a pending record if that is all there is."""
    rows = ContactChannelIdentity.objects.for_workspace(workspace).filter(contact=contact)
    identity = rows.filter(channel_connection=connection).first()
    if identity is not None:
        return identity

    pending = rows.filter(channel_connection__isnull=True, platform=connection.platform).first()
    if pending is None:
        return None
    # Contract 1's lazy upgrade: the address was captured before this connection
    # existed, and this is the first time there is one to attach it to.
    pending.channel_connection = connection
    try:
        with transaction.atomic():
            pending.save()
    except IntegrityError:
        # Another send upgraded it, or a real row already exists for this
        # address on this connection. Either way that row is authoritative.
        return rows.filter(channel_connection=connection).first()
    return pending


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------


def send_outbound(
    *,
    workspace: Any,
    contact: Any,
    connection: Any,
    outbound: OutboundMessage,
    source: str,
    idempotency_key: str,
    blocking: bool = False,
    internal: bool = False,
) -> Message:
    """Contract 1's entry point. Returns the ``Message``; never raises.

    ``blocking`` lets a worker wait briefly for a rate-limit token rather than
    rescheduling immediately; the inline path leaves it False, per SPEC §7.1.
    ``internal`` stores an inbox note without calling anybody (SPEC §14). Both
    are keyword-only with defaults, so contract 1's written signature still
    calls correctly.
    """
    source = MessageSource(source).value
    if not idempotency_key:
        # The unique constraint is partial — ``condition=~Q(idempotency_key="")``
        # — so a blank key is not deduplicated by anything: every call would
        # insert a fresh row and make a fresh provider call, silently voiding
        # the guarantee contract 1 is built on. Raising is deliberate and is the
        # same treatment an unknown ``source`` gets one line up: the never-raises
        # promise covers send *outcomes*, not a caller passing nonsense.
        raise ValueError("send_outbound() needs a non-empty idempotency_key (SPEC §9.4).")
    conversation = open_conversation(workspace=workspace, contact=contact, connection=connection)

    if source == MessageSource.AGENT:
        # Before compliance, deliberately: the pause records an agent *taking
        # over*, and a reply that compliance then refuses is still a takeover.
        _extend_automation_pause(conversation, AGENT_AUTOMATION_PAUSE)

    if internal:
        # A note never reaches a platform, so it skips compliance and the bucket
        # entirely. It is stored as a message because that is what SPEC §14 says
        # it is; ``internal`` is what keeps it out of the send path forever. It
        # is real thread content, so unlike a refused send it does set recency.
        note, created = _record(conversation, outbound, source, idempotency_key, internal=True)
        if created:
            _touch(conversation)
        return note

    identity = _identity_for(workspace, contact, connection)
    if identity is None:
        return _failed(conversation, outbound, source, idempotency_key, Denial.NO_IDENTITY.value)

    decision = can_send(identity, source, outbound)
    if not isinstance(decision, Allowed):
        # Never silently dropped: the flow engine needs a row to follow its
        # `default` edge from, and an operator needs to know what was refused.
        return _failed(conversation, outbound, source, idempotency_key, decision.code)

    on_the_wire = decision.apply(outbound)
    message, created = _record(conversation, on_the_wire, source, idempotency_key)
    if not created and message.status != MessageStatus.QUEUED:
        # Somebody already sent (or failed) this exact message. SPEC §9.4:
        # "on unique violation, skip the call".
        return message

    return _dispatch(message, connection, identity, on_the_wire, blocking=blocking)


def send_as_agent(
    *,
    workspace: Any,
    contact: Any,
    connection: Any,
    outbound: OutboundMessage,
    idempotency_key: str,
    internal: bool = False,
) -> Message:
    """An inbox reply (SPEC §14). Pauses automation; may use HUMAN_AGENT."""
    return send_outbound(
        workspace=workspace,
        contact=contact,
        connection=connection,
        outbound=outbound,
        source=MessageSource.AGENT.value,
        idempotency_key=idempotency_key,
        internal=internal,
    )


def send_via_api(
    *,
    workspace: Any,
    contact: Any,
    connection: Any,
    outbound: OutboundMessage,
    idempotency_key: str,
) -> Message:
    """A send from the public API (#25). Automation rules, no agent allowance."""
    return send_outbound(
        workspace=workspace,
        contact=contact,
        connection=connection,
        outbound=outbound,
        source=MessageSource.API.value,
        idempotency_key=idempotency_key,
    )


def _record(
    conversation: Conversation,
    outbound: OutboundMessage,
    source: str,
    idempotency_key: str,
    *,
    internal: bool = False,
) -> tuple[Message, bool]:
    """Insert the outbound row, or return the one that already owns the key.

    The database is the arbitrator, not a prior read: two callers racing on one
    key both attempt the insert, and the unique index picks a winner. A
    check-then-insert has a window where both see nothing.
    """
    message = Message(
        conversation=conversation,
        direction=MessageDirection.OUT,
        source=source,
        body=outbound.to_body(),
        status=MessageStatus.SENT if internal else MessageStatus.QUEUED,
        idempotency_key=idempotency_key,
        internal=internal,
    )
    try:
        with transaction.atomic():
            message.save()
    except IntegrityError:
        existing = (
            Message.objects.for_workspace(conversation.workspace_id)
            .filter(conversation=conversation, idempotency_key=idempotency_key)
            .get()
        )
        return existing, False
    return message, True


def _failed(
    conversation: Conversation,
    outbound: OutboundMessage,
    source: str,
    idempotency_key: str,
    code: str,
) -> Message:
    """A message row recording a refusal. Contract 1: never a silent drop."""
    message, created = _record(conversation, outbound, source, idempotency_key)
    if not created and message.status != MessageStatus.QUEUED:
        return message
    return _finalize(message, status=MessageStatus.FAILED, error=code)


def _dispatch(
    message: Message,
    connection: Any,
    identity: ContactChannelIdentity,
    outbound: OutboundMessage,
    *,
    blocking: bool,
) -> Message:
    """Resolve the adapter, claim the attempt, take a token, send, record.

    That order is load-bearing and was arrived at the wrong way round once.

    The **adapter first**, because a platform with no adapter installed can never
    send and must not spend anything finding that out. The **claim second**,
    because it is the right to make one provider call and only its winner has
    any use for a token — acquiring first meant every racing caller burned one
    and then lost the claim, draining the connection's bucket with calls that
    never happened. The **token last**, so the only thing that consumes rate is
    a send that is actually about to be attempted.
    """
    try:
        adapter = adapter_for(connection.platform)
    except LookupError:
        return _finalize(message, status=MessageStatus.FAILED, error=Failure.NO_ADAPTER.value)

    if not _claim(message):
        # Another caller owns this attempt. Its outcome is the one that counts.
        message.refresh_from_db()
        return message

    if blocking:
        acquisition = buckets.acquire(connection, max_wait=settings.SEND_BUCKET_MAX_WAIT_SECONDS)
    else:
        acquisition = buckets.try_acquire(connection)
    if isinstance(acquisition, buckets.Deferred):
        # SPEC §7.1: the inline path "falls back to enqueue when empty". Hand
        # the claim back first — no call was made, so the next attempt must be
        # able to take it, and must not be charged an attempt for waiting.
        _release_claim(message)
        return _defer(
            message,
            error=Failure.RATE_DEFERRED.value,
            delay_seconds=acquisition.wait_seconds,
            # Not a failure and not a rung on the backoff ladder: the token is
            # due when the bucket says it is.
            use_backoff=False,
        )

    # The thread's recency is set here rather than when the row was inserted:
    # this is the first point at which a message is genuinely on its way, and a
    # compliance-denied send must not float a conversation to the top of an
    # inbox sorted by last_message_at (SPEC §14).
    _touch(message.conversation)

    try:
        result = adapter.send(connection, identity, outbound)
    except RateLimitError as exc:
        # Before APIError: it is a subclass, and the two are treated oppositely.
        return _defer(message, error=Failure.RATE_LIMITED.value, delay_seconds=exc.retry_after)
    except APIError as exc:
        return _record_api_error(message, exc)
    except Exception:
        # An adapter bug must not kill the flow (SPEC §9.5), and must not be
        # mistaken for the platform rejecting the message.
        logger.exception("Adapter raised while sending message %s", message.pk)
        return _defer(message, error=Failure.PROVIDER_UNAVAILABLE.value)

    if result.status == SendStatus.FAILED:
        return _finalize(message, status=MessageStatus.FAILED, error=_provider_code(result))
    return _finalize(
        message,
        status=MessageStatus.SENT,
        provider_message_id=result.provider_message_id,
    )


def _defer(
    message: Message,
    *,
    error: str,
    delay_seconds: float | None = None,
    use_backoff: bool = True,
) -> Message:
    """Queue the message for another attempt — or leave it failed if there is none.

    The ordering here is the whole point. ``_schedule_retry`` fails the row
    itself when the budget is spent, and an earlier version finalised to
    ``queued`` immediately afterwards regardless: a message that had exhausted
    five attempts came out marked ``queued`` with nothing scheduled to move it,
    stuck forever, with an operator staring at the wrong status. So the status
    is written only when an attempt was actually armed.

    A scheduling error is not allowed to escape either. ``send_outbound``
    promises never to raise for a send outcome (SPEC §9.5: a failed send follows
    the ``default`` edge rather than killing the flow), and "the retry could not
    be scheduled" is a send outcome like any other — so it fails the message
    with its own code instead of leaving it queued and unreferenced.
    """
    try:
        scheduled = _schedule_retry(message, delay_seconds=delay_seconds, use_backoff=use_backoff)
    except Exception:
        logger.exception("Could not schedule a retry for message %s", message.pk)
        return _finalize(message, status=MessageStatus.FAILED, error=Failure.RETRY_UNSCHEDULABLE.value)

    if scheduled is None:
        # _schedule_retry already failed the row with retries_exhausted.
        message.refresh_from_db()
        return message
    return _finalize(message, status=MessageStatus.QUEUED, error=error)


def _record_api_error(message: Message, exc: APIError) -> Message:
    """4xx is permanent; 5xx, a timeout and an unknown status are not (SPEC §9.5)."""
    status_code = exc.status_code
    retryable = status_code is None or status_code >= 500 or status_code in _RETRYABLE_STATUSES
    if not retryable:
        return _finalize(message, status=MessageStatus.FAILED, error=_code(Failure.PROVIDER_REJECTED, exc))
    return _defer(message, error=_code(Failure.PROVIDER_UNAVAILABLE, exc))


def _code(failure: Failure, exc: APIError) -> str:
    """``failure:<provider code>``, with nothing of the provider's prose.

    ``APIError.code`` is the platform's own machine-readable code and is already
    capped at 64 characters by ``apps.channels.providers.base``; the message is
    deliberately not included, because a provider's error text quotes the
    request that caused it (SECURITY-BASELINE §5).
    """
    detail = exc.code or (str(exc.status_code) if exc.status_code is not None else "")
    return f"{failure.value}:{detail}" if detail else failure.value


def _provider_code(result: SendResult) -> str:
    return f"{Failure.PROVIDER_REJECTED.value}:{result.error}" if result.error else Failure.PROVIDER_REJECTED.value


def _claim(message: Message) -> bool:
    """Win the right to call the provider for this message, exactly once.

    A compare-and-set, not a read: the unique key stops a second *row*, and this
    stops a second *call*. ``dispatched_at IS NULL`` re-opens only when
    :func:`_schedule_retry` deliberately clears it.
    """
    claimed = (
        Message.objects.for_workspace(message.workspace_id)
        .filter(pk=message.pk, status=MessageStatus.QUEUED, dispatched_at__isnull=True)
        .update(
            dispatched_at=timezone.now(),
            send_attempts=F("send_attempts") + 1,
            updated_at=timezone.now(),
        )
    )
    if claimed:
        message.refresh_from_db()
    return bool(claimed)


def _release_claim(message: Message) -> None:
    """Hand back a claim taken for an attempt that never reached the provider.

    Only ever called before ``adapter.send``. Clearing ``dispatched_at`` re-opens
    the compare-and-set so the next attempt can take it, and decrementing
    ``send_attempts`` keeps the SPEC §9.5 budget counting provider calls rather
    than trips through this function — a message that waited five times for a
    rate-limit token has not used up its five tries at sending.
    """
    Message.objects.for_workspace(message.workspace_id).filter(pk=message.pk).update(
        dispatched_at=None,
        send_attempts=Greatest(F("send_attempts") - 1, Value(0)),
        updated_at=timezone.now(),
    )
    message.refresh_from_db()


def _finalize(
    message: Message,
    *,
    status: str,
    error: str = "",
    provider_message_id: str = "",
) -> Message:
    """Write the outcome. Survives a provider id that is not unique.

    ``(connection, provider_message_id)`` is unique (SPEC §5), and a platform
    that reused an id — or an adapter that returned a constant — would otherwise
    raise an IntegrityError from inside a function this module promises never
    raises, turning a successfully delivered message into a dead flow. The send
    happened either way, so the status is what matters and the id is what is
    dropped, loudly.
    """
    message.status = status
    message.error = error[:200]
    fields = ["status", "error", "updated_at"]
    if provider_message_id:
        message.provider_message_id = provider_message_id[:200]
        fields.append("provider_message_id")
    try:
        with transaction.atomic():
            message.save(update_fields=fields)
    except IntegrityError:
        logger.warning(
            "Provider message id from %s is already in use on this connection; storing the status without it.",
            message.channel_connection_id,
        )
        message.provider_message_id = ""
        message.save(update_fields=["status", "error", "provider_message_id", "updated_at"])
    return message


def _touch(conversation: Conversation) -> None:
    conversation.last_message_at = timezone.now()
    conversation.save(update_fields=["last_message_at", "updated_at"])


def _schedule_retry(message: Message, *, delay_seconds: float | None = None, use_backoff: bool = True) -> Any:
    """Arm a ``send_retry``, returning the action or None if the budget is spent.

    Imported late to avoid a cycle: the handler imports this module back.
    """
    from apps.messaging.handlers import schedule_send_retry

    return schedule_send_retry(message, delay_seconds=delay_seconds, use_backoff=use_backoff)
