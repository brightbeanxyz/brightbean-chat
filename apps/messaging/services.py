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
from apps.contacts.models import ContactStatus
from apps.messaging import analytics, buckets
from apps.messaging.codes import Denial, Failure
from apps.messaging.compliance import Allowed, can_send

# The single write site for `opted_out_at` (ROADMAP contract 3). No cycle:
# ingest.py reads identities and models, never this module.
from apps.messaging.ingest import apply_opt_in, apply_opt_out
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
    "record_opt_in",
    "record_opt_out",
    "send_as_agent",
    "send_compliance_reply",
    "send_outbound",
    "send_via_api",
    "upsert_contact_identity",
    "withdraw_send",
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


def record_opt_out(identity: ContactChannelIdentity, *, source: str = "") -> bool:
    """Withdraw consent on one identity, by hand. ``True`` when it was not already out.

    The operator-facing half of SPEC §19. A contact who asks a human to stop
    messaging them has withdrawn consent exactly as surely as one who typed STOP,
    and before this the CRM had no audited way to record it: :func:`record_consent`
    only ever *adds* consent, and the inbound path was private.

    **This function does not write ``opted_out_at``.** It delegates to
    :func:`apps.messaging.ingest.apply_opt_out`, which ROADMAP contract 3 makes the
    single write site — so the facade is the door a caller uses without the column
    growing a second writer. Issue #13's contact detail page is the first caller.

    There is deliberately **no matching re-subscribe**. ``opted_out_at`` is set once
    and never cleared from here: SPEC §19 puts opt-out at a chokepoint precisely so
    it cannot be bypassed, and an operator toggle that could un-say it is a bypass
    with a nicer name. Re-consent arrives the way consent did — from the contact,
    through L5-D's keyword hook — not from the team's side of the conversation.

    ``source`` is recorded in the log line only. It does not touch ``opt_in_source``,
    which is the *consent* audit: overwriting "they messaged us" with "an operator
    opted them out" would destroy the record of how permission was obtained at the
    moment it stopped applying, which is the pair a regulator asks to see together.
    """
    changed = apply_opt_out(identity)
    if changed:
        logger.info("Identity %s opted out manually (source=%s)", identity.pk, source or "manual")
    return changed


def record_opt_in(identity: ContactChannelIdentity, *, source: str = OptInSource.MESSAGE_IN) -> bool:
    """Restore consent the contact withdrew. ``True`` when this call changed something.

    The counterpart to :func:`record_opt_out`, and pointedly **not** its mirror
    image: that one is reachable from the CRM, and this one is not. SPEC §19 puts
    opt-out at a chokepoint so it cannot be bypassed, so re-consent has to come
    from the contact — a channel adapter's re-subscribe keyword (SPEC §6.6's
    ``START``/``UNSTOP``), never a toggle on the team's side of the conversation.
    ``record_opt_out``'s own docstring names that asymmetry and this function is
    the other end of it.

    **It does not write ``opted_out_at``.** It delegates to
    :func:`apps.messaging.ingest.apply_opt_in`, which ROADMAP contract 3 makes
    the single write site, so the facade is the door a caller uses without the
    column growing a second writer.
    """
    changed = apply_opt_in(identity, source=source)
    if changed:
        logger.info("Identity %s opted back in (source=%s)", identity.pk, source)
    return changed


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

    Connection resolution is contract 1's, spelled out: **one identity row per
    active connection of that platform**; if none exists at capture time, a
    connection-less *pending* record is stored and upgraded lazily at first send
    (that upgrade lives in :func:`_identity_for`, because "first send" is when a
    connection is finally in hand).

    All of them, not the first one. A workspace can legitimately run two
    Telegram bots or two WhatsApp numbers, and attaching a captured address to
    only the oldest meant a send through any of the others failed with
    ``no_identity`` — for a contact whose address the workspace demonstrably
    held. Passing ``connection`` explicitly narrows it to that one, which is
    what a caller who already knows the channel wants.

    Returns the identity for ``connection`` when one was given, and otherwise
    the one on the workspace's oldest active connection, so the return value is
    stable across calls.

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

    targets = [connection] if connection is not None else _active_connections(contact.workspace_id, platform)
    if not targets:
        # Nothing to attach it to yet. One pending row, upgraded at first send.
        return _upsert_one(contact, platform, address, None, source=source, opt_in=opt_in, adopt_pending=True)

    identities: list[ContactChannelIdentity] = []
    for index, target in enumerate(targets):
        identities.append(
            _upsert_one(
                contact,
                platform,
                address,
                target,
                source=source,
                opt_in=opt_in,
                # A pending row can only be adopted once — it becomes one real
                # row — so the rest are created outright.
                adopt_pending=index == 0,
            )
        )
    return identities[0]


def _active_connections(workspace_id: Any, platform: str) -> list[Any]:
    """Every active connection of ``platform``, oldest first."""
    from apps.channels.models import ChannelConnection, ConnectionStatus

    return list(
        ChannelConnection.objects.for_workspace(workspace_id)
        .filter(platform=platform, status=ConnectionStatus.ACTIVE)
        .order_by("created_at")
    )


def _upsert_one(
    contact: Any,
    platform: str,
    address: str,
    connection: Any,
    *,
    source: str,
    opt_in: bool,
    adopt_pending: bool,
) -> ContactChannelIdentity:
    """One identity row, created or refreshed."""
    rows = ContactChannelIdentity.objects.for_workspace(contact.workspace_id).filter(
        contact=contact, platform=platform, platform_user_id=address
    )
    if connection is None:
        identity = rows.filter(channel_connection__isnull=True).first()
    else:
        identity = rows.filter(channel_connection=connection).first()
        if identity is None and adopt_pending:
            identity = rows.filter(channel_connection__isnull=True).first()
            if identity is not None:
                identity.channel_connection = connection

    if identity is None:
        identity = ContactChannelIdentity(
            contact=contact,
            channel_connection=connection,
            platform=platform,
            platform_user_id=address,
        )

    if opt_in and identity.opted_out_at is None:
        identity.opt_in = True
        identity.opt_in_at = identity.opt_in_at or timezone.now()
        identity.opt_in_source = identity.opt_in_source or source or OptInSource.MANUAL
    identity.save()
    return identity


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


def send_compliance_reply(
    *,
    workspace: Any,
    contact: Any,
    connection: Any,
    outbound: OutboundMessage,
    idempotency_key: str,
) -> Message:
    """A reply the law requires us to send, exempt from opt-out suppression only.

    The one sanctioned way past :func:`~apps.messaging.compliance.can_send`, and
    it exists because the alternative is worse. SPEC §6.6 requires an SMS
    ``STOP`` to be answered with a confirmation and a ``HELP`` to be answered at
    all — both after the identity is already suppressed, and both are carrier
    obligations rather than messages anybody chose to send. Without a door here
    an adapter would have to call ``adapter.send`` itself, and the confirmation
    would then have no message row, no idempotency key and no rate token: an
    agent looking at the thread would see ``HELP`` and no answer.

    **Exempt from the compliance verdict, and from nothing else.** The
    conversation, the message row, SPEC §9.4's idempotency key, the connection's
    token bucket, the tombstone check on a deleted contact and the adapter
    dispatch are all the ordinary path — this function is ``send_outbound``
    minus one call. In particular a send with no identity still fails with
    ``no_identity``: there is no address to answer, and being mandatory does not
    conjure one.

    It carries **no platform knowledge**, deliberately. Contract 4's promise is
    that a Layer-5 platform costs one module and one registry line, and a branch
    in here naming SMS would be the first crack in it. What makes a reply
    mandatory is the caller's business; L5-E's unsubscribe confirmation is the
    next one through this door.

    ``source`` is fixed to ``automation``. SPEC §5 fixes the vocabulary at
    ``automation, agent, api, broadcast, sequence`` and none of them means
    "the law made us", so the closest true one is used rather than a sixth value
    invented here. It emphatically must not be ``agent``: that would pause
    automation on the conversation for thirty minutes because somebody typed
    STOP, and nobody has taken anything over.
    """
    source = MessageSource.AUTOMATION.value
    if not idempotency_key:
        raise ValueError("send_compliance_reply() needs a non-empty idempotency_key (SPEC §9.4).")

    conversation = open_conversation(workspace=workspace, contact=contact, connection=connection)
    identity = _identity_for(workspace, contact, connection)
    if identity is None:
        return _failed(conversation, outbound, source, idempotency_key, Denial.NO_IDENTITY.value)

    message, created = _record(conversation, outbound, source, idempotency_key)
    if not created and message.status != MessageStatus.QUEUED:
        # Somebody already sent this exact reply. A redelivered STOP must not
        # produce a second confirmation (SPEC §9.4).
        return message
    return _dispatch(message, connection, identity, outbound, blocking=False)


def withdraw_send(message: Message, *, reason: str) -> Message:
    """Retract a send that was accepted but never handed to the adapter.

    The other end of contract 1 from :func:`send_outbound`. Some work that
    produced a message can be cancelled after the row exists — a broadcast
    stopped mid-fanout, a sequence unsubscribed while a step waits on the token
    bucket — and until this existed the caller could cancel the ``send_retry``
    but had no way to say so on the message. What it left behind is the exact
    state :func:`_defer` calls out: ``queued`` with nothing scheduled to move it,
    stuck forever, with an operator staring at the wrong status.

    **Compare-and-set, on ``queued`` and an unset ``dispatched_at``.** Not a
    read-then-save: ``dispatched_at`` is what :func:`_claim` grants, so a message
    the adapter has already been handed is one whose outcome belongs to whoever
    holds that claim, and a retraction that overwrote it would replace a real
    send with a fiction — including SPEC §9.4's unknown outcome, the one state
    where the platform may well have the message. Losing the race is not an
    error and not a raise: the message comes back with whatever status the
    winner wrote, which is the truth about it. Callers that need to know can
    compare ``status``.

    ``reason`` is the caller's own machine-readable word for *why*, stored as
    ``withdrawn:<reason>`` in the same shape as :func:`_code` — the code before
    the colon is what ``codes.describe()`` renders, so the detail costs an
    operator nothing and gives whoever is debugging the cancellation a thread to
    pull. It is required rather than defaulted because "cancelled by what?" is
    the only question a withdrawn row raises.

    The pending ``send_retry`` goes with it, and only once the compare-and-set
    has won — cancelling first would disarm the ladder under a send that is
    genuinely still in flight. The two cannot then disagree: the status is
    written before the action is cancelled, and ``handle_send_retry`` re-reads
    that status, so even a worker that claims the row in the window between them
    finds a message it must not send.
    """
    error = _code(Failure.WITHDRAWN, reason.strip())
    withdrawn = (
        Message.objects.for_workspace(message.workspace_id)
        .filter(pk=message.pk, status=MessageStatus.QUEUED, dispatched_at__isnull=True)
        .update(status=MessageStatus.FAILED, error=error, updated_at=timezone.now())
    )
    if not withdrawn:
        message.refresh_from_db()
        return message
    _cancel_retry(message)
    message.status = MessageStatus.FAILED
    message.error = error
    # A withdrawal is a send that never happened, and SPEC §5 has one word for
    # that outcome. The compare-and-set above is what makes it countable exactly
    # once (issue #26).
    analytics.record_status(message, previous=MessageStatus.QUEUED, current=MessageStatus.FAILED)
    return message


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
    """Refuse a tombstone, resolve the adapter, claim, take a token, send, record.

    That order is load-bearing and was arrived at the wrong way round once.

    The **tombstone check first**, because it is free and because everything
    after it spends something — an adapter lookup, an attempt, a rate token.

    The **adapter first**, because a platform with no adapter installed can never
    send and must not spend anything finding that out. The **claim second**,
    because it is the right to make one provider call and only its winner has
    any use for a token — acquiring first meant every racing caller burned one
    and then lost the claim, draining the connection's bucket with calls that
    never happened. The **token last**, so the only thing that consumes rate is
    a send that is actually about to be attempted.
    """
    if message.conversation.contact.status != ContactStatus.ACTIVE:
        # The last gate before a provider call, and the only one that catches a
        # contact deleted *after* the message was queued. `send_outbound` cannot
        # do it alone: `handle_send_retry` re-enters here directly, so a pending
        # send_retry for somebody an operator removed an hour ago would still
        # reach the platform. Issue #13's delete cancels those queue rows, but
        # the invariant belongs here rather than resting on every caller of
        # `delete_contact` remembering to tidy up.
        #
        # `_finalize_if_queued` rather than `_finalize`: this runs before
        # `_claim()`, with `dispatched_at` still NULL — precisely the window
        # `withdraw_send` can also match, and an unconditional save here could
        # silently overwrite a withdrawal that won that race first.
        finalized = _finalize_if_queued(message, status=MessageStatus.FAILED, error=Denial.CONTACT_DELETED.value)
        return finalized or _reread(message)

    try:
        adapter = adapter_for(connection.platform)
    except LookupError:
        finalized = _finalize_if_queued(message, status=MessageStatus.FAILED, error=Failure.NO_ADAPTER.value)
        return finalized or _reread(message)

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

    **The rate-deferral caller reopens a second race, on top of the first.**
    ``_dispatch`` clears ``dispatched_at`` (:func:`_release_claim`) before
    calling here, and that is exactly the compare-and-set
    :func:`withdraw_send` needs — for the span of this one call, a message mid
    rate-deferral looks identical to one nobody is touching. A withdrawal that
    lands in that span must not be resurrected by an unconditional write: the
    message would come back ``queued`` under a retry the withdrawal had already
    cancelled (or is about to), which is the exact "stuck forever" state this
    function exists to prevent, reached through the one door built to prevent
    it. So both final writes below go through :func:`_finalize_if_queued`
    rather than :func:`_finalize` — a compare-and-set on ``status=QUEUED``,
    true going in from every caller — and losing it means a withdrawal won
    first; the retry just armed is cancelled rather than left to outlive the
    message it belongs to.
    """
    try:
        scheduled = _schedule_retry(message, delay_seconds=delay_seconds, use_backoff=use_backoff)
    except Exception:
        logger.exception("Could not schedule a retry for message %s", message.pk)
        return _finalize_if_queued(
            message, status=MessageStatus.FAILED, error=Failure.RETRY_UNSCHEDULABLE.value
        ) or _reread(message)

    if scheduled is None:
        # _schedule_retry already failed the row with retries_exhausted.
        message.refresh_from_db()
        return message

    finalized = _finalize_if_queued(message, status=MessageStatus.QUEUED, error=error)
    if finalized is None:
        # Lost the race: something else — only withdraw_send can, per the
        # docstring above — already finalised this message. Its outcome wins,
        # and the retry just armed is not it.
        _cancel_retry(message)
        return _reread(message)
    return finalized


def _finalize_if_queued(message: Message, *, status: str, error: str = "") -> Message | None:
    """Write a terminal or re-queued outcome, but only while it is still ``queued``.

    Every call site here runs before a claim is (re)taken: the two checks at
    the top of :func:`_dispatch` (before :func:`_claim`), ``handle_send_retry``'s
    identity and compliance re-checks and its own give-up-on-budget check
    (before it reopens the claim), and both of :func:`_defer`'s own final
    writes on the rate-deferral path, where :func:`_release_claim` has just
    cleared ``dispatched_at``. That is exactly the window
    :func:`withdraw_send`'s compare-and-set targets, and an earlier version of
    this module reached every one of these outcomes through :func:`_finalize`'s
    unconditional save — which let a concurrent withdrawal's reason be
    silently overwritten by whichever of the two ran second. The *status*
    stayed right either way (still ``failed``, no resurrection, no
    double-send), but the *reason* — the whole point of a withdrawal being
    auditable — was not.

    A compare-and-set closes it: ``status=queued`` is true of every message
    reaching any of these call sites, by construction of when they run, so
    filtering on it costs nothing on the ordinary path and catches exactly the
    race. Returns ``None`` on the loss rather than the message, so a caller
    cannot mistake "someone else already decided" for "I decided". Sets the
    written fields on the Python object directly rather than refreshing from
    the database on the win — every value here is already known, and
    ``cancel_send_retry``'s own docstring expects ``withdraw_send`` to run once
    per broadcast recipient, where an avoidable extra ``SELECT`` is not free.
    """
    updated = (
        Message.objects.for_workspace(message.workspace_id)
        .filter(pk=message.pk, status=MessageStatus.QUEUED)
        .update(status=status, error=error[:200], updated_at=timezone.now())
    )
    if not updated:
        return None
    message.status = status
    message.error = error[:200]
    # Only on the win. A compare-and-set that lost its race changed nothing, and
    # the winner has already counted whatever it wrote (issue #26).
    analytics.record_status(message, previous=MessageStatus.QUEUED, current=status)
    return message


def _reread(message: Message) -> Message:
    """The row's true current state, after losing a compare-and-set to
    whatever else already decided this message's fate."""
    message.refresh_from_db()
    return message


def _record_api_error(message: Message, exc: APIError) -> Message:
    """4xx is permanent; 5xx, a timeout and an unknown status are not (SPEC §9.5)."""
    status_code = exc.status_code
    retryable = status_code is None or status_code >= 500 or status_code in _RETRYABLE_STATUSES
    detail = exc.code or (str(status_code) if status_code is not None else "")
    if not retryable:
        return _finalize(message, status=MessageStatus.FAILED, error=_code(Failure.PROVIDER_REJECTED, detail))
    return _defer(message, error=_code(Failure.PROVIDER_UNAVAILABLE, detail))


def _code(failure: Failure, detail: str) -> str:
    """``failure:<detail>``, capped, with nothing more added.

    Sixty-four characters, the limit ``apps.channels.providers.base`` already
    puts on a platform's own code — the same budget applies whether the detail
    came from a provider's error or from a caller's own withdrawal reason
    (:func:`withdraw_send`), so one cap serves both. ``error`` holds 200 total
    and :func:`_finalize`/:func:`_finalize_if_queued` truncate there too, but
    nothing here should be spending that whole budget on free-form text
    (SECURITY-BASELINE §5) — a code that stays short stays greppable in a log.
    """
    detail = detail[:64]
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
    previous = message.status
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
    # After the write, not before: a counter for a status that failed to store
    # would be a number with nothing behind it (issue #26).
    analytics.record_status(message, previous=previous, current=status)
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


def _cancel_retry(message: Message) -> int:
    """Disarm a pending ``send_retry``. Imported late for the same cycle."""
    from apps.messaging.handlers import cancel_send_retry

    return cancel_send_retry(message)
