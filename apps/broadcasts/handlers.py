"""The two queue action types a broadcast runs on (SPEC §13.2).

``broadcast_fanout`` splits an audience; ``broadcast_send`` delivers one copy.
Both names were reserved in ``apps.queueing.models.ActionType`` before this app
existed, and registration is an import side effect of ``BroadcastsConfig.ready()``
— the pattern ``apps.queueing.registry``'s docstring writes out.

Neither registration passes ``replace=True``. The registry raises on a second
handler for one type on purpose — ``apps.queueing.registry``'s docstring calls it
out: "two apps quietly claiming one type is a bug that would otherwise surface as
work running under the wrong code" — and switching that off to buy nothing is how
the guard stops guarding. Re-importing this module is safe without it, because
the guard compares identity and a cached module hands back the same function.

Three things the queue guarantees, so nothing here re-does them: the handler runs
inside a transaction that already holds the contact advisory lock when the row
names a contact; raising retries on SPEC §15's backoff ladder; returning normally
marks the row done in the same transaction as the work.

--------------------------------------------------------------------------
Why the fanout re-enqueues itself instead of looping
--------------------------------------------------------------------------

SPEC §13.2 says "in batches of 500". Read as a loop inside one handler that would
be a single action holding a transaction open for a ten-thousand-contact
audience — past ``ZOMBIE_AFTER`` (10 minutes), at which point another worker's
sweep returns the row to ``pending`` and runs it a second time.

So a chunk is an *action*. Each one writes five hundred recipients, arms their
sends, updates the counters once, and enqueues its successor with a cursor. That
bounds every transaction, lets the worker interleave transactional work between
chunks, and gives cancellation a checkpoint every five hundred contacts. It is
the same self-rescheduling shape ``apps.queueing.housekeeping`` uses for the
hourly chain.

--------------------------------------------------------------------------
How a 10k fanout avoids starving transactional automation
--------------------------------------------------------------------------

The claim query is ``WHERE status='pending' AND run_at <= now() ORDER BY run_at
LIMIT 50``. Ten thousand rows all due *now* would therefore fill every batch for
minutes, and a flow resume scheduled a second later would wait behind them.

So ``run_at`` is **spread**: the i-th send of a broadcast is due at
``started_at + i / rate``, where ``rate`` is
``apps.messaging.buckets.rate_for(platform)`` — the connection's own configured
send rate. At any instant only about ``rate`` of the broadcast's rows are due, so
an ordinary action (always due "now") sorts ahead of the rest of the fanout and
is claimed in the next cycle.

This is **not a second throttle.** It adds no bucket, no counter and no sleep:
it schedules the queue to arrive at roughly the rate the bucket would grant
anyway, so ``send_outbound``'s own acquire — the one and only throttle — rarely
has to defer. When it does defer, the existing ``send_retry`` path handles it,
exactly as it does for every other sender.
"""

import logging
from datetime import timedelta
from typing import Any
from uuid import UUID

from django.utils import timezone

from apps.broadcasts import audience as audience_module
from apps.broadcasts import services
from apps.broadcasts.models import (
    Broadcast,
    BroadcastRecipient,
    BroadcastStatus,
    RecipientStatus,
)
from apps.channels.events import OutboundMessage
from apps.channels.models import ConnectionStatus
from apps.contacts.models import Contact
from apps.messaging import buckets
from apps.messaging.codes import Denial
from apps.messaging.compliance import Allowed, can_send
from apps.messaging.models import ContactChannelIdentity, Message, MessageSource, MessageStatus
from apps.queueing.models import ActionType, ScheduledAction
from apps.queueing.registry import register_handler, schedule

logger = logging.getLogger(__name__)

__all__ = ["CHUNK_SIZE", "handle_broadcast_fanout", "handle_broadcast_send"]

#: SPEC §13.2: "inserts one broadcast_send action per contact in batches of 500".
#:
#: What a chunk costs is worth knowing before changing this number. Every send is
#: one ``queueing.registry.schedule`` call, and a call carrying an idempotency key
#: wraps its insert in a savepoint — so a chunk is roughly fifteen hundred
#: statements, and a ten-thousand-contact broadcast is thirty thousand spread
#: across twenty chunks. That is a deliberate trade: the queue's public API is
#: idempotent per row, and bulk-inserting ``ScheduledAction`` here to save it
#: would be a second write path into a table this app does not own.
CHUNK_SIZE = 500


# ---------------------------------------------------------------------------
# Fanout
# ---------------------------------------------------------------------------


@register_handler(ActionType.BROADCAST_FANOUT)
def handle_broadcast_fanout(payload: dict[str, Any], action: ScheduledAction) -> None:
    """Resolve one chunk of the audience and arm its sends.

    Payload: ``broadcast_id``, optional ``cursor`` (the last contact id the
    previous chunk saw).

    Everything is re-resolved from ids inside the workspace that owns the row. A
    payload is a document that has been sitting in a table, so treating its ids
    as ids rather than as trusted objects is what keeps a scheduled action from
    reaching across tenants.
    """
    broadcast = _load_broadcast(payload, action)
    if broadcast is None:
        return
    if broadcast.status == BroadcastStatus.CANCELLED:
        # Half one of cancellation: stop scheduling. The successor row this
        # handler would have enqueued is never written, so the audience beyond
        # this cursor is never expanded at all.
        logger.info("Broadcast %s was cancelled; fanout stops here.", broadcast.pk)
        return
    if broadcast.status not in (BroadcastStatus.SCHEDULED, BroadcastStatus.SENDING):
        logger.warning("Broadcast %s is %s; fanout has nothing to do.", broadcast.pk, broadcast.status)
        return

    cursor = _uuid(payload.get("cursor"))
    if broadcast.status == BroadcastStatus.SCHEDULED:
        broadcast.status = BroadcastStatus.SENDING
        broadcast.started_at = broadcast.started_at or timezone.now()
        broadcast.save(update_fields=["status", "started_at", "updated_at"])

    candidates = list(audience_module.iter_candidates(broadcast, after=cursor, limit=CHUNK_SIZE))
    if candidates:
        _write_chunk(broadcast, candidates)
        cursor = candidates[-1].contact_id

    current = services.release_stats(broadcast)

    if len(candidates) == CHUNK_SIZE:
        schedule(
            ActionType.BROADCAST_FANOUT,
            timezone.now(),
            {"broadcast_id": str(broadcast.pk), "cursor": str(cursor)},
            workspace=broadcast.workspace,
            # Keyed on the cursor, so a re-run of this chunk — which zombie
            # recovery can force — arms the same successor rather than a second
            # one, and the successor's own recipient inserts are idempotent
            # anyway through unique (broadcast, contact).
            idempotency_key=f"broadcast:{broadcast.pk}:fanout:{cursor}",
        )
        return

    # The audience is exhausted. An audience that was entirely skipped has
    # nothing pending and finishes here rather than waiting for a send that will
    # never run — and ``exclude_action_id`` is what lets it: this handler's own
    # row is ``running`` for the length of its transaction, and
    # ``services.fanout_outstanding`` would otherwise read it as work still owed.
    if current.is_finished:
        services.settle(broadcast, exclude_action_id=action.pk)


def _write_chunk(broadcast: Broadcast, candidates: list[Any]) -> None:
    """Insert this chunk's recipient rows, then arm a send for the eligible ones.

    The recipient rows go in first and in bulk. ``ignore_conflicts`` plus
    ``unique (broadcast, contact)`` is what makes a forced re-run of this handler
    insert nothing the second time — a guarantee that does not depend on the
    queue's idempotency key, so the two are independent belts (SPEC §21).
    """
    now = timezone.now()
    anchor = broadcast.started_at or now
    rate = buckets.rate_for(broadcast.platform)

    BroadcastRecipient.objects.bulk_create(
        [
            BroadcastRecipient(
                # bulk_create bypasses save(), so the workspace is set here.
                workspace_id=broadcast.workspace_id,
                broadcast=broadcast,
                contact_id=candidate.contact_id,
                identity_id=candidate.identity_id,
                status=RecipientStatus.PENDING if candidate.is_eligible else RecipientStatus.SKIPPED,
                reason="" if candidate.is_eligible else candidate.decision,
            )
            for candidate in candidates
        ],
        ignore_conflicts=True,
    )

    # Re-read rather than trusting bulk_create's return: with ignore_conflicts
    # Postgres returns no primary keys, and on a re-run some of these rows are
    # the *previous* run's and may already have been sent.
    pending = {
        row.contact_id: row
        for row in BroadcastRecipient.objects.for_workspace(broadcast.workspace_id).filter(
            broadcast=broadcast,
            status=RecipientStatus.PENDING,
            contact_id__in=[candidate.contact_id for candidate in candidates if candidate.is_eligible],
        )
    }

    # Where this chunk starts in the broadcast's overall ordering, so the run_at
    # spread continues across chunks instead of restarting at the anchor every
    # five hundred contacts. Counted from the rows rather than carried in the
    # payload: a payload counter would double-count on a re-run.
    offset = BroadcastRecipient.objects.for_workspace(broadcast.workspace_id).filter(broadcast=broadcast).exclude(
        status=RecipientStatus.SKIPPED
    ).count() - len(pending)

    for index, candidate in enumerate(c for c in candidates if c.is_eligible):
        recipient = pending.get(candidate.contact_id)
        if recipient is None:
            # Already sent by a previous run of this chunk. Nothing to arm.
            continue
        schedule(
            ActionType.BROADCAST_SEND,
            anchor + timedelta(seconds=(offset + index) / rate),
            {
                "broadcast_id": str(broadcast.pk),
                "recipient_id": str(recipient.pk),
                "contact_id": str(candidate.contact_id),
            },
            workspace=broadcast.workspace,
            # Naming the contact takes the advisory lock for free, so a broadcast
            # send cannot interleave with a flow step for the same person
            # (SPEC §9.6).
            contact=candidate.contact_id,
            # SPEC §13.2's key, verbatim. One send per contact per broadcast,
            # whatever re-runs this.
            idempotency_key=f"broadcast:{broadcast.pk}:contact:{candidate.contact_id}",
        )


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------


@register_handler(ActionType.BROADCAST_SEND)
def handle_broadcast_send(payload: dict[str, Any], action: ScheduledAction) -> None:
    """Deliver one contact's copy, re-checking everything that can have moved.

    Payload: ``broadcast_id``, ``recipient_id``, ``contact_id``.

    Returns normally in every case the queue can do nothing about — a cancelled
    broadcast, a recipient another run already settled, a contact compliance now
    refuses. Raising would retry work that cannot succeed, and the outcome is
    recorded on the recipient row either way.
    """
    broadcast = _load_broadcast(payload, action)
    if broadcast is None:
        return
    recipient = _load_recipient(payload, action, broadcast)
    if recipient is None:
        return

    if recipient.status != RecipientStatus.PENDING:
        # A re-run after the handler committed but before the row was marked
        # done. The outcome already recorded is the one that counts — and this
        # check comes **before** the cancellation branch deliberately. A row that
        # was already ``running`` when a cancel landed can be returned to
        # ``pending`` by zombie recovery and re-claimed; if the cancellation
        # branch ran first it would rewrite a recipient that had already been
        # delivered to ``cancelled``, losing the record of a message the contact
        # demonstrably received and contradicting "already-sent stand".
        return
    if broadcast.status == BroadcastStatus.CANCELLED:
        # Half two of cancellation, and the one that matters most: this row was
        # already ``running`` when the cancel landed, so the bulk flip could not
        # reach it. A claimed action has to refuse itself.
        settle_recipient(recipient, RecipientStatus.CANCELLED, "")
        return

    contact = _scoped(Contact, action.workspace_id, payload.get("contact_id"))
    if contact is None:
        settle_recipient(recipient, RecipientStatus.SKIPPED, Denial.CONTACT_DELETED.value)
        _maybe_settle(broadcast)
        return

    connection = broadcast.channel_connection
    if connection.status == ConnectionStatus.DISABLED:
        # Re-read at send time, because the composer's own selector already
        # refuses a disabled connection (composer.broadcastable_connections) and
        # hours can pass between that choice and this row being claimed.
        # Switching a channel off is the most explicit instruction an operator
        # has for "stop using this", and nothing downstream enforces it: the
        # facade takes the connection object it is handed, and adapter_for keys
        # on the platform. So it has to be refused here or the stored
        # credentials go on being used.
        settle_recipient(recipient, RecipientStatus.SKIPPED, Denial.NO_CONNECTION.value)
        _maybe_settle(broadcast)
        return

    identity = _identity_for(broadcast, contact, recipient)
    if identity is None:
        settle_recipient(recipient, RecipientStatus.SKIPPED, Denial.NO_IDENTITY.value)
        _maybe_settle(broadcast)
        return

    probe = audience_module.probe_for(broadcast)
    decision = can_send(identity, MessageSource.BROADCAST.value, probe)
    if not isinstance(decision, Allowed):
        # SPEC §13.2's send-time re-check. A contact who opted out between fanout
        # and send lands here, and is *counted* rather than quietly dropped.
        settle_recipient(recipient, RecipientStatus.SKIPPED, decision.code)
        _maybe_settle(broadcast)
        return

    if audience_module.suppressed(broadcast.workspace_id, identity.platform_user_id):
        # The email suppression list, re-read at send time for the same reason
        # compliance is: hours can pass, and a bounce recorded in between should
        # stop this send rather than be discovered by the provider.
        settle_recipient(recipient, RecipientStatus.SKIPPED, Denial.OPTED_OUT.value)
        _maybe_settle(broadcast)
        return

    if broadcast.whatsapp_template_id is not None:
        message = _send_template(broadcast, contact, connection, probe)
    else:
        message = _send_mini_flow(broadcast, contact, connection)

    _record_send(recipient, message)
    _maybe_settle(broadcast)


def _send_template(broadcast: Broadcast, contact: Any, connection: Any, probe: OutboundMessage) -> Message | None:
    """Template content goes straight through contract 1's facade.

    Three things happen here that a flow's ``send_message`` node also does, in
    the same order and through the same functions.

    **The pairing is re-checked.** ``whatsapp_templates.sendable`` refuses a
    template that is no longer approved, or that belongs to another connection —
    and its docstring explains why the second case is the dangerous one: a
    template name is scoped to the WhatsApp Business Account, so if the other
    number happens to hold one with the same name and language, Meta sends *that*
    one. The right shape, approved, the wrong words, to a real contact, with
    nothing reporting a problem. Hours can pass between scheduling and this send.

    **The slot values are rendered per contact**, through
    ``apps.flows.rendering`` — the one shared, engine-free substitution
    (SECURITY-BASELINE §3). That is what lets an operator map a slot to
    ``{{first_name}}``, and it is the reason the adapter receives finished
    strings and its docstring tells it never to render them again.

    **The readable copy is stored on the message row.** The adapter puts only the
    template reference on the wire — Meta holds the approved words — so a row
    carrying nothing would leave the inbox showing a blank message the contact
    demonstrably received. ``rendered_text`` is the same substitution the
    composer's preview uses, so the two cannot disagree.

    ``blocking=True`` is the worker path SPEC §8 describes — "the worker respects
    buckets" — bounded by ``SEND_BUCKET_MAX_WAIT_SECONDS``, past which the facade
    turns the wait into a ``send_retry`` rather than sleeping with a transaction
    open.

    Nothing here writes a ``Message``: the facade is the only send site, which is
    what ``apps/messaging/tests/test_write_sites.py`` asserts over the AST.
    """
    from apps.channels import whatsapp_templates
    from apps.channels.events import TextBlock
    from apps.flows.rendering import context_for, render
    from apps.messaging.services import send_outbound

    template = whatsapp_templates.sendable(broadcast.whatsapp_template_id, connection)
    if template is None:
        logger.warning(
            "Broadcast %s names a template that is no longer sendable on connection %s.",
            broadcast.pk,
            connection.pk,
        )
        return None

    context = context_for(contact)
    values = {str(slot): render(str(value), context) for slot, value in (broadcast.template_variables or {}).items()}
    text = whatsapp_templates.rendered_text(template, values)
    outbound = OutboundMessage(
        blocks=(TextBlock(text=text),) if text else (),
        tag=probe.tag,
        # The row's reference, not the composer's: a template renamed since it was
        # picked resolves to what it is now rather than to what it was.
        template_ref=template.reference,
        template_variables=tuple(sorted(values.items())),
    )
    return send_outbound(
        workspace=broadcast.workspace,
        contact=contact,
        connection=connection,
        outbound=outbound,
        source=MessageSource.BROADCAST.value,
        idempotency_key=f"broadcast:{broadcast.pk}:contact:{contact.pk}",
        blocking=True,
    )


def _send_mini_flow(broadcast: Broadcast, contact: Any, connection: Any) -> Message | None:
    """Mini-flow content runs as a one-shot flow start, so buttons behave normally.

    ``started_by="broadcast:<id>"`` is what the engine's send envelope reads to
    give the message ``source="broadcast"`` and this broadcast's compliance tag
    (``apps.flows.engine.sending``). Without it the send would go out as ordinary
    automation with no tag, and every outside-window Messenger recipient would be
    refused at the last moment.

    The version is the one pinned at schedule time, and ``preview=False`` says so
    explicitly: an unpublished version would otherwise be *derived* as a test run
    and kept out of the analytics counters.
    """
    from apps.flows.engine import FlowNotRunnableError, start_flow
    from apps.flows.engine.sending import ENVELOPE_TAG_VAR
    from apps.flows.messaging import message_idempotency_key
    from apps.flows.models import StartedBy

    if broadcast.flow is None or broadcast.flow_version is None:
        logger.warning("Broadcast %s has no pinned content; nothing to send.", broadcast.pk)
        return None

    try:
        execution = start_flow(
            contact,
            broadcast.flow,
            started_by=StartedBy.stamp(StartedBy.BROADCAST, broadcast.pk),
            flow_version=broadcast.flow_version,
            connection=connection,
            preview=False,
            variables={ENVELOPE_TAG_VAR: broadcast.message_tag} if broadcast.message_tag else None,
        )
    except FlowNotRunnableError as exc:
        # Not retriable: five attempts over six hours cannot make an empty graph
        # runnable, and the recipient is better recorded as failed than as a row
        # the queue keeps re-attempting.
        logger.warning("Broadcast %s cannot run for contact %s: %s", broadcast.pk, contact.pk, exc)
        return None

    # The engine's key is deterministic, which is what lets the message be found
    # from the row rather than returned through three layers of node interface.
    key = message_idempotency_key(execution, services.CONTENT_NODE_ID, 0)
    return Message.objects.for_workspace(broadcast.workspace_id).filter(idempotency_key=key).first()


def _record_send(recipient: BroadcastRecipient, message: Message | None) -> None:
    """Attach the message and record the outcome it reports.

    A message that is still ``queued`` counts as sent: it is on its way, a
    ``send_retry`` is armed for it, and the recipient's counters follow the
    message row from here on — including a delivery receipt, which
    ``apps.messaging.ingest`` writes to the same column with no help from this
    app.
    """
    if message is None:
        settle_recipient(recipient, RecipientStatus.FAILED, "")
        return
    if message.status == MessageStatus.FAILED:
        settle_recipient(recipient, RecipientStatus.FAILED, message.error, message=message)
        return
    settle_recipient(recipient, RecipientStatus.SENT, "", message=message)


def settle_recipient(
    recipient: BroadcastRecipient,
    status: str,
    reason: str,
    *,
    message: Message | None = None,
) -> None:
    recipient.status = status
    recipient.reason = (reason or "")[:200]
    fields = ["status", "reason", "updated_at"]
    if message is not None:
        recipient.message = message
        fields.append("message")
    recipient.save(update_fields=fields)


def _maybe_settle(broadcast: Broadcast) -> None:
    """Finish the broadcast if this was the last outstanding recipient.

    One indexed ``EXISTS`` before the counter aggregate, because this runs once
    per send and the aggregate does not need to.
    """
    still_pending = (
        BroadcastRecipient.objects.for_workspace(broadcast.workspace_id)
        .filter(broadcast=broadcast, status=RecipientStatus.PENDING)
        .exists()
    )
    if not still_pending:
        services.settle(broadcast)


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def _load_broadcast(payload: dict[str, Any], action: ScheduledAction) -> Broadcast | None:
    broadcast = _scoped(Broadcast, action.workspace_id, payload.get("broadcast_id"))
    if broadcast is None:
        logger.warning("Action %s names a broadcast that is gone; dropping it.", action.pk)
    return broadcast


def _load_recipient(
    payload: dict[str, Any], action: ScheduledAction, broadcast: Broadcast
) -> BroadcastRecipient | None:
    recipient = _scoped(BroadcastRecipient, action.workspace_id, payload.get("recipient_id"))
    if recipient is None or recipient.broadcast_id != broadcast.pk:
        logger.warning("Action %s names a recipient that is gone or belongs elsewhere; dropping it.", action.pk)
        return None
    return recipient


def _identity_for(
    broadcast: Broadcast, contact: Any, recipient: BroadcastRecipient | None = None
) -> ContactChannelIdentity | None:
    """The identity this send goes to — the one fanout chose, wherever it still exists.

    Fanout already answered this question, and answered it *better*:
    ``audience.iter_candidates`` ranks a contact's candidate identities
    eligible-first, then connection-bound, then by id. Re-deriving it here with a
    different rule is how a preview and a send come apart. A contact holding two
    addresses on one connection — two numbers, or an address re-captured after a
    merge — could have the clean one counted as eligible by fanout and the
    opted-out one picked here, so the send is refused for a reason nothing
    actually changed and the operator sees an unexplained skip.

    So the recorded identity wins whenever it is still usable. It is re-read
    rather than trusted: hours can pass, and an identity can be deleted or
    reassigned to another connection in between, in which case this falls back to
    resolving one the way the facade would.
    """
    rows = ContactChannelIdentity.objects.for_workspace(broadcast.workspace_id).filter(contact=contact)
    if recipient is not None and recipient.identity_id is not None:
        recorded = rows.filter(pk=recipient.identity_id).first()
        if recorded is not None and recorded.channel_connection_id in (
            broadcast.channel_connection_id,
            None,
        ):
            return recorded
    return (
        rows.filter(channel_connection=broadcast.channel_connection).order_by("pk").first()
        or rows.filter(channel_connection__isnull=True, platform=broadcast.platform).order_by("pk").first()
    )


def _scoped(model: Any, workspace_id: Any, raw_id: Any) -> Any:
    """Fetch one row by id, inside ``workspace_id``, or ``None``.

    Scoped rather than ``.get(pk=...)``: the ids live in a JSON payload, and a
    lookup that crosses tenants because a payload said so is the same hole
    ``get_scoped_object_or_404`` closes on the request side.
    """
    if not raw_id or workspace_id is None:
        return None
    try:
        pk = UUID(str(raw_id))
    except (ValueError, AttributeError, TypeError):
        return None
    return model.objects.for_workspace(workspace_id).filter(pk=pk).first()


def _uuid(raw: Any) -> UUID | None:
    if not raw:
        return None
    try:
        return UUID(str(raw))
    except (ValueError, AttributeError, TypeError):
        return None
