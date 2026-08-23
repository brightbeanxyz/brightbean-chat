"""Composing, scheduling, cancelling and settling a broadcast (SPEC §13).

Everything that mutates a broadcast lives here; the views translate a request
into one of these calls and render the result. Three parts are worth reading
before changing anything.

--------------------------------------------------------------------------
The content is a real flow, kept out of the way
--------------------------------------------------------------------------

SPEC §5 gives a broadcast ``flow_id`` **or** ``whatsapp_template_id``. The
mini-flow half is a genuine ``flows.Flow`` holding a single-node ``graph_json``
(ROADMAP line 43: "HTMX composer (single-node graph_json — no React embed)"),
written through ``apps.flows.services.save_draft`` so it goes through the same
sanitiser every other graph does.

It lives in a reserved folder, :data:`BROADCAST_FOLDER`, so the flow list groups
every broadcast's private copy under one heading an operator can filter away —
and it is **archived when the broadcast finishes**, which takes it out of that
list altogether (``apps.flows.views._visible_flows`` excludes archived flows by
default). Archiving it any earlier is not an option: ``start_flow`` refuses an
archived flow outright, which is the right rule for the engine and the reason the
timing here is what it is. Neither half needs a column added to ``apps.flows`` or
that app learning what a broadcast is.

:func:`schedule_broadcast` **publishes** it and pins the published version. The
foreign key alone would not be enough: ``apps.flows.services.save_draft`` rewrites
the latest version *in place* while it is unpublished, so an edit during a drain
would change the copy the rest of the audience receives. Publishing is what makes
the next edit open a new version instead.

--------------------------------------------------------------------------
Refusals are values, not exceptions to swallow
--------------------------------------------------------------------------

:class:`BroadcastError` is raised for everything an operator can fix — an empty
audience, a Messenger send with no tag, a graph that will not validate — and the
views turn it into a toast. It is a ``ValueError`` subclass so a caller that
forgets is still not going to mistake it for success.

--------------------------------------------------------------------------
Counters come from the rows, not from a running total
--------------------------------------------------------------------------

:func:`counters` is two aggregate queries over ``BroadcastRecipient``, both
served by the ``(broadcast, status)`` index. ``stats`` is written back only when
it changed — SPEC §13.2's "updated in batches" — so a page polling every three
seconds does not put an UPDATE on a GET.
"""

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from apps.broadcasts import events as broadcast_events
from apps.broadcasts.models import (
    LIVE_STATUSES,
    Broadcast,
    BroadcastRecipient,
    BroadcastStatus,
    RecipientStatus,
)
from apps.broadcasts.notifications import EVENT_BROADCAST_FINISHED
from apps.channels import policy as channel_policy
from apps.channels import whatsapp_templates
from apps.contacts import conditions
from apps.flows import services as flow_services
from apps.flows.models import Flow, FlowStatus, FlowVersion
from apps.messaging.codes import Denial
from apps.messaging.models import MessageStatus
from apps.queueing.models import ActionStatus, ActionType, ScheduledAction
from apps.queueing.registry import schedule

logger = logging.getLogger(__name__)

__all__ = [
    "BroadcastError",
    "BROADCAST_FOLDER",
    "CONTENT_NODE_ID",
    "Counters",
    "WINDOW_DENIALS",
    "cancel_broadcast",
    "content_graph",
    "counters",
    "create_broadcast",
    "delete_broadcast",
    "duplicate_broadcast",
    "node_config",
    "release_stats",
    "save_content",
    "save_template",
    "schedule_broadcast",
    "set_audience",
    "set_channel",
    "settle",
]


class BroadcastError(ValueError):
    """Something an operator can fix. The views render it as a toast."""


#: Where a broadcast's private mini-flow is filed in the flow list. A folder
#: rather than a hidden flag, because the flow *is* real and an operator asking
#: "what did that broadcast actually send?" deserves an answer — but it is one
#: heading rather than a copy loose among their own automations. Moving a flow
#: out of it is how an operator adopts it (see :func:`delete_broadcast`).
BROADCAST_FOLDER = "Broadcasts"

#: The id of the one node a broadcast's graph holds. Fixed rather than generated
#: because it also names the send's idempotency key through
#: ``apps.flows.messaging.message_idempotency_key`` — a generated id would make
#: the key unreproducible from the row, which is what attaches a ``Message`` to
#: its recipient. Matches ``apps.flows.schema.envelope.ID_PATTERN``.
CONTENT_NODE_ID = "broadcast"

#: Denials that mean "the messaging window is shut". SPEC §5's ``stats`` json
#: names one counter, ``skipped_window``; these are the three codes SPEC §8 can
#: produce for that state, kept as a set so a fourth escape hatch on a future
#: platform lands in the right bucket by being added here once.
WINDOW_DENIALS: frozenset[str] = frozenset(
    {Denial.OUTSIDE_WINDOW.value, Denial.NEEDS_TAG.value, Denial.NEEDS_TEMPLATE.value}
)

#: Message statuses that mean the platform confirmed delivery, and reading.
#: ``read`` implies ``delivered`` — the ladder only moves forward
#: (``apps.messaging.ingest``) — so a read message is counted under both.
_DELIVERED_STATUSES = frozenset({"delivered", "read"})
_READ_STATUSES = frozenset({"read"})
#: And the one that means it never arrived, whatever the recipient row says.
_FAILED_STATUSES = frozenset({"failed"})


# ---------------------------------------------------------------------------
# Composing
# ---------------------------------------------------------------------------


def create_broadcast(*, workspace: Any, name: str, connection: Any, user: Any = None) -> Broadcast:
    """A new draft on one connection.

    The connection is required at creation because every later step depends on
    it: which blocks the content step offers, whether a tag selector appears,
    and which policy the audience preview is computed against.
    """
    if not _may_broadcast(connection):
        raise BroadcastError("This channel does not allow broadcasts.")
    broadcast = Broadcast(workspace=workspace, name=name.strip()[:200], channel_connection=connection, created_by=user)
    broadcast.save()
    return broadcast


def _may_broadcast(connection: Any) -> bool:
    """Contract 4's flag, asked of the policy table rather than of a name."""
    return channel_policy.policy_for(connection.platform).broadcast_allowed


def set_channel(broadcast: Broadcast, connection: Any) -> Broadcast:
    """Move a draft to another connection, discarding what no longer applies.

    A tag belongs to the platform that accepts it and a template to the
    connection it was approved on, so both are cleared rather than carried: a
    Messenger tag left on a WhatsApp broadcast would be refused at send time, at
    which point the operator is no longer looking.
    """
    _require_draft(broadcast)
    if not _may_broadcast(connection):
        raise BroadcastError("This channel does not allow broadcasts.")
    broadcast.channel_connection = connection
    broadcast.message_tag = ""
    broadcast.whatsapp_template = None
    broadcast.template_variables = {}
    broadcast.save(
        update_fields=["channel_connection", "message_tag", "whatsapp_template", "template_variables", "updated_at"]
    )
    return broadcast


def set_audience(broadcast: Broadcast, *, filter_json: Any, segment: Any = None) -> Broadcast:
    """Store the targeting document, validated by contract 8's own validator.

    ``segment`` is provenance only. The document is copied out of the segment
    rather than referenced, because a segment edited after a broadcast was
    scheduled must not change who the broadcast goes to — and because a segment
    deleted mid-send would otherwise take the audience with it.
    """
    _require_draft(broadcast)
    if not filter_json:
        # An empty document is the absence of a filter, not a filter matching
        # nobody, and the condition engine cannot compile one. "Everyone" has a
        # spelling — ``{"match": "all", "rules": []}`` — and choosing it is an
        # act the composer makes somebody perform, count in hand.
        raise BroadcastError("Add at least one rule, or pick a saved segment.")
    try:
        conditions.validate(broadcast.workspace_id, filter_json)
    except conditions.ConditionError as exc:
        raise BroadcastError(str(exc)) from exc
    broadcast.target_filter_json = filter_json
    broadcast.segment = segment
    broadcast.save(update_fields=["target_filter_json", "segment", "updated_at"])
    return broadcast


def content_graph(config: dict[str, Any]) -> dict[str, Any]:
    """One ``send_message`` node, in SPEC §9.1's envelope and nothing else.

    The config's *shape* is the flow builder's — ``apps.flows.schema.nodes``'s
    ``message_block``, ``message_button`` and ``quick_reply`` fragments — because
    reusing the data shapes is what lets the same validator, the same sanitiser
    and the same engine node serve a broadcast. What is not reused is the canvas:
    a broadcast is one message and there is nothing to lay out.
    """
    return {
        "schema": 1,
        "nodes": [{"id": CONTENT_NODE_ID, "type": "send_message", "position": {"x": 0, "y": 0}, "config": config}],
        "edges": [],
    }


def save_content(broadcast: Broadcast, config: dict[str, Any], *, user: Any = None) -> Broadcast:
    """Write the composed message into this broadcast's private mini-flow.

    Creating the flow lazily, on the first save, keeps a broadcast an operator
    abandoned at step one from leaving a flow row behind.
    """
    _require_draft(broadcast)
    graph = content_graph(config)
    result = flow_services.validate_for_workspace(graph, broadcast.workspace)
    if not result.is_publishable:
        raise BroadcastError(_first_error(result))

    with transaction.atomic():
        flow = broadcast.flow
        if flow is None:
            flow = flow_services.create_flow(
                workspace=broadcast.workspace,
                name=f"Broadcast: {broadcast.name}"[:200],
                folder=BROADCAST_FOLDER,
                user=user,
            )
        flow_services.save_draft(flow, graph, user=user)
        broadcast.flow = flow
        # Content is one or the other (SPEC §5), and the check constraint agrees.
        broadcast.whatsapp_template = None
        broadcast.template_variables = {}
        broadcast.save(update_fields=["flow", "whatsapp_template", "template_variables", "updated_at"])
    return broadcast


def _first_error(result: Any) -> str:
    """The first blocking finding, as a sentence for a toast."""
    for issue in getattr(result, "errors", ()) or ():
        message = getattr(issue, "message", "")
        if message:
            return str(message)
    return "That message cannot be sent as written."


def save_template(broadcast: Broadcast, template: Any, variables: dict[str, str]) -> Broadcast:
    """Point the broadcast at an approved template and its variable mapping.

    The template must belong to this workspace *and* this connection — the
    caller resolves it with ``get_scoped_object_or_404`` and this re-checks the
    connection, because a template approved on another number is not sendable on
    this one and the platform would refuse it at the last possible moment.
    """
    _require_draft(broadcast)
    if template.channel_connection_id != broadcast.channel_connection_id:
        raise BroadcastError("That template was approved on a different channel.")

    # Every slot the approved copy declares needs a value. Meta rejects a
    # template message whose parameter count does not match the one it reviewed,
    # so the alternative to checking here is discovering it once per recipient —
    # and the slots come from ``slots_for``, the same reading of
    # ``body_structure`` the composer built its form from.
    values = {str(k): str(v) for k, v in (variables or {}).items()}
    missing = [slot for slot in whatsapp_templates.slots_for(template) if not values.get(slot, "").strip()]
    if missing:
        raise BroadcastError(f"This template needs a value for {', '.join(missing)}.")

    with transaction.atomic():
        broadcast.whatsapp_template = template
        broadcast.template_variables = values
        broadcast.flow = None
        broadcast.flow_version = None
        broadcast.save(update_fields=["whatsapp_template", "template_variables", "flow", "flow_version", "updated_at"])
    return broadcast


def set_tag(broadcast: Broadcast, tag: str) -> Broadcast:
    """Set the outside-window message tag, checked against the platform's own list.

    Validated here as well as by the compliance engine, and deliberately: a tag
    the platform does not accept would otherwise be stored, previewed as if it
    worked, and refused ten thousand times at send. The list comes from
    ``PlatformPolicy.outside_window``, never from a literal.
    """
    _require_draft(broadcast)
    tag = (tag or "").strip()
    outside = channel_policy.policy_for(broadcast.platform).outside_window
    allowed = outside.tags if isinstance(outside, channel_policy.NeedsTag) else ()
    if tag and tag not in allowed:
        raise BroadcastError("That message tag is not one this channel accepts.")
    broadcast.message_tag = tag
    broadcast.save(update_fields=["message_tag", "updated_at"])
    return broadcast


def duplicate_broadcast(broadcast: Broadcast, *, user: Any = None) -> Broadcast:
    """Copy a broadcast back to draft, content and all.

    Never inherits a status, a schedule or any counters: sending is an act, and a
    copy that arrived already scheduled would send itself.
    """
    with transaction.atomic():
        copy = Broadcast(
            workspace=broadcast.workspace,
            name=f"{broadcast.name[:193]} (copy)",
            channel_connection=broadcast.channel_connection,
            target_filter_json=broadcast.target_filter_json,
            segment=broadcast.segment,
            whatsapp_template=broadcast.whatsapp_template,
            template_variables=broadcast.template_variables,
            message_tag=broadcast.message_tag,
            created_by=user,
        )
        copy.save()
        source = broadcast.flow
        if source is not None:
            latest = flow_services.latest_version(source)
            if latest is not None:
                copy = save_content(copy, node_config(latest.graph_json), user=user)
    return copy


def node_config(graph: Any) -> dict[str, Any]:
    """The single node's config out of a stored graph, or an empty one.

    The composer re-opens a draft through this, and :func:`duplicate_broadcast`
    copies one through it, so "where does the message live inside the envelope"
    is answered once.
    """
    nodes = graph.get("nodes") if isinstance(graph, dict) else None
    for node in nodes or ():
        if isinstance(node, dict) and isinstance(node.get("config"), dict):
            return dict(node["config"])
    return {}


def delete_broadcast(broadcast: Broadcast) -> None:
    """Delete a draft or a finished broadcast, and its private mini-flow with it.

    A live broadcast is refused rather than cascaded: deleting one mid-send would
    orphan the queue rows that are about to look for it, and "cancel, then
    delete" is a sequence an operator can follow.
    """
    if broadcast.is_live:
        raise BroadcastError("Cancel this broadcast before deleting it.")
    flow = broadcast.flow
    with transaction.atomic():
        broadcast.delete()
        if flow is not None and flow.folder == BROADCAST_FOLDER:
            # Only the private copy. A flow an operator moved out of the reserved
            # folder is theirs now — that move is the adoption — and it outlives
            # the broadcast that made it.
            Flow.objects.for_workspace(flow.workspace_id).filter(pk=flow.pk).delete()


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------


def schedule_broadcast(broadcast: Broadcast, *, when: datetime | None = None, preview: Any = None) -> Broadcast:
    """Validate, pin the content, and put the fanout in the queue.

    ``preview`` is the :class:`apps.broadcasts.audience.AudiencePreview` the
    composer has just shown, passed in rather than recomputed so the numbers an
    operator agreed to are the numbers this decision is made on. Omitted, it is
    computed here — the API path has no composer in front of it.

    The gates are all compliance-derived, never platform-named: a Messenger
    audience with people outside the window and no valid tag is refused because
    ``needs_tag`` has a non-zero count, and a WhatsApp one because
    ``needs_template`` does. Both are the same annotation the preview came from.
    """
    from apps.broadcasts import audience as audience_module

    if broadcast.status != BroadcastStatus.DRAFT:
        raise BroadcastError("Only a draft can be scheduled.")
    if broadcast.flow_id is None and broadcast.whatsapp_template_id is None:
        raise BroadcastError("Add a message before sending.")
    if not broadcast.target_filter_json:
        # Reachable through the API, where nothing walks the wizard's steps.
        raise BroadcastError("Choose who this broadcast goes to.")

    counts = preview if preview is not None else audience_module.preview(broadcast)
    if counts.total == 0:
        raise BroadcastError("Nobody matches this audience.")

    # Before the generic refusal, deliberately. When the whole audience is
    # outside the window, "nobody can be messaged" and "this needs a message tag"
    # are both true — and only the second one tells an operator what to do about
    # it. The specific reason wins.
    _refuse_window_gaps(broadcast, counts)

    if counts.eligible == 0:
        raise BroadcastError("Nobody in this audience can be messaged on this channel right now.")

    when = when or broadcast.scheduled_at or timezone.now()
    with transaction.atomic():
        locked = Broadcast.objects.for_workspace(broadcast.workspace_id).select_for_update().get(pk=broadcast.pk)
        if locked.status != BroadcastStatus.DRAFT:
            raise BroadcastError("This broadcast is already on its way.")
        locked.flow_version = _pin_version(locked)
        locked.scheduled_at = when
        locked.status = BroadcastStatus.SCHEDULED
        locked.stats = {}
        locked.save(update_fields=["flow_version", "scheduled_at", "status", "stats", "updated_at"])

        schedule(
            ActionType.BROADCAST_FANOUT,
            when,
            {"broadcast_id": str(locked.pk)},
            # The Workspace instance, not its id: schedule() assigns straight to
            # the FK and an id there raises.
            workspace=locked.workspace,
            # No contact: fanout is about a set, not a person, so there is no
            # advisory lock to take and nothing it could usefully serialise on.
            idempotency_key=f"broadcast:{locked.pk}:fanout:start",
        )
    broadcast.refresh_from_db()
    return broadcast


def _refuse_window_gaps(broadcast: Broadcast, counts: Any) -> None:
    """SPEC §6.4 and §6.5's composer gates, expressed as counts rather than names.

    A tag or a template does not become *required* because of the platform's
    name; it becomes required because somebody in this audience is outside the
    window and the policy's only escape is that one. So the condition is the
    count of that denial, and the copy comes from the policy row.
    """
    needs_tag = counts.needs(Denial.NEEDS_TAG.value)
    if needs_tag:
        outside = channel_policy.policy_for(broadcast.platform).outside_window
        text = outside.allowed_use_text if isinstance(outside, channel_policy.NeedsTag) else ""
        raise BroadcastError(
            f"{needs_tag} of these contacts are outside the messaging window, so this send needs a "
            f"non-promotional message tag. {text}".strip()
        )
    needs_template = counts.needs(Denial.NEEDS_TEMPLATE.value)
    if needs_template:
        raise BroadcastError(
            f"{needs_template} of these contacts are outside the messaging window, so this send needs "
            f"an approved template."
        )


def _pin_version(broadcast: Broadcast) -> FlowVersion | None:
    """The exact graph this send will use, frozen now.

    A template broadcast has no flow and pins nothing. A mini-flow one is
    **published**, and the foreign key alone is not what freezes it —
    ``apps.flows.services.save_draft`` updates the latest version *in place*
    while it is unpublished, so an edit during a drain would rewrite the very row
    the broadcast pointed at and the rest of the audience would receive different
    copy. Publishing is what makes the next edit open version 2 instead.

    Publishing also validates strictly, which is the right last gate: the content
    step validated what was typed, and this re-checks what is about to be sent to
    thousands of people.

    It leaves the flow ``active``, deliberately. ``start_flow`` refuses an
    archived flow outright — a correct rule for the engine — so the private copy
    has to stay runnable for as long as the queue might reach it.
    :func:`_retire_flow` archives it when the broadcast comes to rest, which is
    the first moment that is safe. Nothing here sets ``published`` by hand, which
    ``apps.flows.models`` asks callers not to do.
    """
    if broadcast.flow is None:
        return None
    try:
        result = flow_services.publish(broadcast.flow, user=broadcast.created_by)
    except flow_services.FlowValidationError as exc:
        raise BroadcastError(_first_error(exc.result)) from exc
    return result.version


def _require_draft(broadcast: Broadcast) -> None:
    if broadcast.status != BroadcastStatus.DRAFT:
        raise BroadcastError("This broadcast has already been sent or scheduled.")


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


def cancel_broadcast(broadcast: Broadcast) -> Broadcast:
    """Stop a broadcast: no more scheduling, and no send that has not already run.

    Four writes, in one transaction, and each covers a case the others cannot.

    1. The **status**, under a row lock. This is what the fanout handler and the
       send handler both re-read, and it is the half of cancellation that covers
       an action a worker has *already claimed* — such a row is ``running`` and
       cannot be flipped from here, so it has to refuse itself.
    2. The **pending queue rows**, flipped to ``cancelled``. SPEC §13.2 asks for
       exactly this, and without it ten thousand handler invocations would each
       run only to discover there is nothing to do.
    3. The **pending recipient rows**, so the counters reconcile immediately
       rather than after a sweep.
    4. The **deferred sends** — see :func:`_cancel_deferred_sends`, the narrowest
       of the four and the one that is easiest to miss.

    Messages already sent stand. There is no unsend.
    """
    with transaction.atomic():
        locked = Broadcast.objects.for_workspace(broadcast.workspace_id).select_for_update().get(pk=broadcast.pk)
        if locked.status not in LIVE_STATUSES:
            raise BroadcastError("Only a scheduled or sending broadcast can be cancelled.")

        locked.status = BroadcastStatus.CANCELLED
        locked.finished_at = timezone.now()
        locked.save(update_fields=["status", "finished_at", "updated_at"])

        # Matched on the payload rather than on the idempotency key's prefix: a
        # LIKE over a btree index in a non-C collation would not use it anyway,
        # and this predicate rides the (status, run_at) index the claim query
        # already maintains.
        ScheduledAction.objects.for_workspace(locked.workspace_id).filter(
            type__in=(ActionType.BROADCAST_FANOUT, ActionType.BROADCAST_SEND),
            status=ActionStatus.PENDING,
            payload__broadcast_id=str(locked.pk),
        ).update(status=ActionStatus.CANCELLED, updated_at=timezone.now())

        BroadcastRecipient.objects.for_workspace(locked.workspace_id).filter(
            broadcast=locked, status=RecipientStatus.PENDING
        ).update(status=RecipientStatus.CANCELLED, updated_at=timezone.now())

        _cancel_deferred_sends(locked)
        release_stats(locked)
        _retire_flow(locked)
    broadcast.refresh_from_db()
    return broadcast


def _cancel_deferred_sends(broadcast: Broadcast) -> int:
    """Stop the sends the facade accepted but the token bucket never released.

    The gap this closes is small and real. A ``broadcast_send`` that ran while
    the connection's bucket was empty does not fail: SPEC §8 has the facade queue
    the message and arm a ``send_retry`` instead. The recipient is recorded as
    sent — it is on its way — and the queue row that cancellation flips is
    ``broadcast_send``, which has already run. So without this, a broadcast
    cancelled at that moment would still deliver a handful of messages minutes
    later, when the retry fired.

    So the retries are cancelled too, matched by the message ids this broadcast
    owns. A message row left ``queued`` with nothing scheduled to move it is
    exactly what its ``error`` says it is — deferred, and then stopped — and it
    stays visible in the thread as a message that never went out. Marking it
    ``failed`` would be truer still, and needs a door contract 1 does not have:
    :mod:`apps.messaging.services` exposes no way to withdraw a send, and adding
    one is that app's change to make, not this one's.

    Returns how many retries were stopped.
    """
    stalled = list(
        BroadcastRecipient.objects.for_workspace(broadcast.workspace_id)
        .filter(broadcast=broadcast, status=RecipientStatus.SENT, message__status=MessageStatus.QUEUED)
        .values_list("pk", "message_id")
    )
    if not stalled:
        return 0

    cancelled = (
        ScheduledAction.objects.for_workspace(broadcast.workspace_id)
        .filter(
            type=ActionType.SEND_RETRY,
            status=ActionStatus.PENDING,
            payload__message_id__in=[str(message_id) for _, message_id in stalled],
        )
        .update(status=ActionStatus.CANCELLED, updated_at=timezone.now())
    )
    BroadcastRecipient.objects.for_workspace(broadcast.workspace_id).filter(pk__in=[pk for pk, _ in stalled]).update(
        status=RecipientStatus.CANCELLED, updated_at=timezone.now()
    )

    logger.info("Broadcast %s: stopped %s deferred send(s) on cancellation", broadcast.pk, cancelled)
    return int(cancelled)


# ---------------------------------------------------------------------------
# Counters
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Counters:
    """SPEC §5's ``stats`` json, computed from the rows that hold the truth."""

    #: Everyone fanout has written a row for so far. The denominator.
    queued: int = 0
    pending: int = 0
    sent: int = 0
    delivered: int = 0
    read: int = 0
    failed: int = 0
    skipped: int = 0
    cancelled: int = 0
    #: SPEC §5 names this counter specifically: skips caused by a shut window.
    skipped_window: int = 0
    #: Denial code -> count, for the "why?" panel. Copy comes from
    #: ``apps.messaging.codes.describe`` at render time.
    skips: dict[str, int] = field(default_factory=dict)

    @property
    def settled(self) -> int:
        """Everyone whose outcome is final."""
        return self.sent + self.failed + self.skipped + self.cancelled

    @property
    def is_finished(self) -> bool:
        """Whether every recipient has reached a terminal state.

        An **empty** broadcast counts as finished, and that is not a corner case
        to shrug at: ``schedule_broadcast`` refuses an audience nobody matches,
        but the audience is resolved again at fanout, and a workspace can delete
        every one of those contacts in between. Requiring ``queued > 0`` here left
        that broadcast at ``sending`` with nothing pending — and the housekeeping
        sweep could not rescue it either, because it asks this same question.
        """
        return self.pending == 0

    @property
    def percent(self) -> int:
        """How far the progress bar has moved, 0–100."""
        return int(self.settled * 100 / self.queued) if self.queued else 0

    def as_stats(self) -> dict[str, Any]:
        return asdict(self)


def counters(broadcast: Broadcast) -> Counters:
    """The live figures: three aggregates, all on the ``(broadcast, status)`` index.

    Deliberately not a running total maintained by the send handler. The worker
    wraps each handler in one transaction, so a counter column incremented in
    there would hold a row lock across the provider call and serialise every send
    in the broadcast behind one row.

    **The message row has the last word on a send that already went out.** A
    recipient is marked ``sent`` the moment the facade accepts the message, which
    is right — it is on its way — but a ``send_retry`` can exhaust its budget
    hours later and leave that message ``failed``. Reading the joined status back
    is what keeps ``delivered`` live *and* keeps a message that ultimately failed
    from being reported as delivered. It is also why this app needs no
    delivery-receipt path of its own: ``apps.messaging.ingest`` advances that
    column and these numbers follow.
    """
    rows = BroadcastRecipient.objects.for_workspace(broadcast.workspace_id).filter(broadcast=broadcast)

    by_status: dict[str, int] = {}
    for status_row in rows.values("status").annotate(n=Count("id")):
        by_status[str(status_row["status"])] = int(status_row["n"])

    skips: dict[str, int] = {}
    for skip_row in rows.filter(status=RecipientStatus.SKIPPED).values("reason").annotate(n=Count("id")):
        skips[str(skip_row["reason"]) or "unknown"] = int(skip_row["n"])

    sent = by_status.get(RecipientStatus.SENT, 0)
    failed = by_status.get(RecipientStatus.FAILED, 0)
    delivered = read = 0
    for message_row in (
        rows.filter(status=RecipientStatus.SENT, message__isnull=False)
        .values("message__status")
        .annotate(n=Count("id"))
    ):
        status, n = str(message_row["message__status"]), int(message_row["n"])
        if status in _FAILED_STATUSES:
            # Recorded as sent, ended failed. Counted where it belongs so
            # queued = sent + failed + cancelled + skipped still holds.
            sent -= n
            failed += n
            continue
        if status in _DELIVERED_STATUSES:
            delivered += n
        if status in _READ_STATUSES:
            read += n

    return Counters(
        queued=sum(by_status.values()),
        pending=by_status.get(RecipientStatus.PENDING, 0),
        sent=sent,
        delivered=delivered,
        read=read,
        failed=failed,
        skipped=by_status.get(RecipientStatus.SKIPPED, 0),
        cancelled=by_status.get(RecipientStatus.CANCELLED, 0),
        skipped_window=sum(n for code, n in skips.items() if code in WINDOW_DENIALS),
        skips=skips,
    )


def release_stats(broadcast: Broadcast, *, current: Counters | None = None) -> Counters:
    """Materialise the counters onto the row, writing only when they changed.

    SPEC §13.2's "updated in batches" read literally: a detail page polling every
    three seconds must not put an UPDATE on every GET, and a fanout chunk of five
    hundred writes this once rather than five hundred times.
    """
    current = current or counters(broadcast)
    stats = current.as_stats()
    if broadcast.stats != stats:
        broadcast.stats = stats
        broadcast.save(update_fields=["stats", "updated_at"])
    return current


# ---------------------------------------------------------------------------
# Settling
# ---------------------------------------------------------------------------


def settle(broadcast: Broadcast) -> bool:
    """Finish a broadcast whose last recipient has reached a terminal state.

    Returns whether this call is the one that finished it. The transition is
    guarded by a conditional UPDATE rather than a read-then-write: several
    ``broadcast_send`` handlers can find the queue empty at the same instant, and
    exactly one of them may emit ``broadcast.finished``. A webhook subscriber
    receiving the event twice would double-count in somebody's CRM.
    """
    current = counters(broadcast)
    if not current.is_finished or broadcast.status != BroadcastStatus.SENDING:
        return False

    finished_at = timezone.now()
    claimed = (
        Broadcast.objects.for_workspace(broadcast.workspace_id)
        .filter(pk=broadcast.pk, status=BroadcastStatus.SENDING)
        .update(
            status=BroadcastStatus.SENT,
            finished_at=finished_at,
            stats=current.as_stats(),
            updated_at=finished_at,
        )
    )
    if not claimed:
        return False

    broadcast.refresh_from_db()
    _retire_flow(broadcast)
    _announce(broadcast, current)
    return True


def _retire_flow(broadcast: Broadcast) -> None:
    """Archive the private mini-flow now that nothing will run it again.

    This is what actually takes it out of the flow list; until now it had to stay
    runnable, because ``start_flow`` refuses an archived flow. Only the copy still
    in the reserved folder is touched — one an operator moved out is theirs.
    """
    flow = broadcast.flow
    if flow is None or flow.folder != BROADCAST_FOLDER or flow.status == FlowStatus.ARCHIVED:
        return
    flow_services.archive_flow(flow)


def _announce(broadcast: Broadcast, current: Counters) -> None:
    """Emit the catalog event and put a row in the operator's bell.

    Both inside the caller's transaction, deliberately. The catalog module's own
    rule is that emitting is synchronous and not deferred to ``on_commit``, so a
    settle that rolls back takes the queued webhook delivery with it — no
    subscriber hears about a broadcast that did not finish.

    A failure to notify must not roll back the settle, though: the broadcast
    *did* finish, and a notification engine that is unhappy is not a reason to
    make the worker retry ten thousand recipients' worth of bookkeeping.
    """
    broadcast_events.emit(
        broadcast_events.EVENT_BROADCAST_FINISHED,
        workspace_id=broadcast.workspace_id,
        broadcast_id=broadcast.pk,
    )
    if broadcast.created_by_id is None:
        return
    try:
        from apps.notifications.engine import notify

        notify(
            broadcast.workspace,
            EVENT_BROADCAST_FINISHED,
            users=[broadcast.created_by],
            context={
                "broadcast_name": broadcast.name,
                "sent": current.sent,
                "failed": current.failed,
                "skipped": current.skipped,
            },
        )
    except Exception:  # noqa: BLE001 - a bell that will not ring is not a failed send
        logger.exception("Could not notify %s that broadcast %s finished", broadcast.created_by_id, broadcast.pk)
