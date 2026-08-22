"""The runner loop: SPEC §9.2's state machine, §9.6's lock, §9.5's failure policy.

    The runner loop: load execution (locked, 9.6), dispatch current node, follow
    edge for the returned handle, increment blocks_since_pause, repeat. Missing
    edge for a handle -> End. […] If it reaches 30 -> Fail("loop cap"), notify
    workspace admins (in-app notification), status failed.

Three properties this module is built around, in order of how badly they break
things when they are wrong.

**One step at a time per contact.** SPEC §3 calls it the "single invariant that
everything relies on". Every public entry point here opens ``transaction.atomic()``
and takes ``apps.queueing.locks.contact_lock`` before it reads the execution, and
nothing in this app advances an execution any other way. Advisory locks are
counted per session, so the worker having already taken the same lock (it does —
``apps.queueing.worker.process_action``) makes the second acquisition free rather
than a deadlock, which is what lets these functions be honest about their own
requirements instead of trusting a caller to have done it.

**That lock blocks, and the inline path must not.** SPEC §9.6: "Inline path:
``pg_try_advisory_xact_lock``; if unavailable, enqueue instead of blocking the
web request." Calling :func:`start_flow` or
:func:`apps.flows.engine.waits.attempt_resume` straight from a webhook request
would hold a gunicorn thread behind whatever the worker is doing to that
contact. The counted-lock property above is what makes the fix two lines rather
than a second set of entry points — take the non-blocking lock first, and the
engine's own acquisition inside it is then free::

    with transaction.atomic(), try_contact_lock(contact) as acquired:
        if not acquired:
            schedule(ActionType.RESUME_EXECUTION, timezone.now(), ..., contact=contact)
            return
        attempt_resume(execution, event)

The worker needs none of this: it has no client on the other end of the socket
and is expected to wait.

**The runner owns every write to the execution row.** Nodes report through
:mod:`apps.flows.engine.results` and never save. That is the reason status can be
reasoned about at all: there is exactly one function per terminal state, each
called from one place.

**Unknown exceptions are not caught here.** A node that raises rolls the whole
transaction back — its own half-finished writes included — and the queue retries
it on SPEC §15's backoff ladder. Catching and marking the execution ``failed``
would do the opposite: it would *commit* the partial writes of the node that just
blew up, in the same transaction, and call the run cleanly dead. So a node
converts the failures it can *diagnose* into ``Fail`` (a filter that no longer
validates, a target flow that has been deleted) and lets everything else
propagate. The inline path's "any error: enqueue" (SPEC §7.1) is L4-A's catch,
not this module's.
"""

import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from apps.common.logging import scrub
from apps.common.uuid7 import uuid7
from apps.contacts.errors import WorkspaceMismatchError
from apps.flows import events
from apps.flows.engine.context import NodeContext
from apps.flows.engine.graph import Graph
from apps.flows.engine.registry import node_class_for
from apps.flows.engine.results import Continue, End, Fail, Schedule, StartNext, StepResult, Wait
from apps.flows.models import LIVE_STATUSES, ExecutionStatus, Flow, FlowExecution, FlowVersion, StartedBy
from apps.flows.services import published_version
from apps.queueing.locks import contact_lock
from apps.queueing.models import ActionStatus, ActionType, ScheduledAction
from apps.queueing.registry import schedule

__all__ = [
    "LOOP_CAP",
    "MAX_STORED_ERROR_CHARS",
    "EngineError",
    "FlowNotRunnableError",
    "advance",
    "locked_execution",
    "resume_execution",
    "start_flow",
]

logger = logging.getLogger(__name__)

#: SPEC §9.2: "If it reaches 30 -> Fail('loop cap')". Blocks executed since the
#: last Wait or Schedule; a flow that loops without ever pausing hits this.
LOOP_CAP = 30

#: ``last_error`` is read by humans in the admin and may quote a hostile value.
#: Same bound and the same reasoning as ``apps.queueing.worker``'s.
MAX_STORED_ERROR_CHARS = 2000

#: The error text a loop-capped run is stored with. A constant because the
#: notification branch keys off the fact, not off matching this string.
LOOP_CAP_ERROR = f"loop cap: {LOOP_CAP} blocks ran without pausing"

#: Fields the runner may write. One tuple, used by every save, so a new column
#: cannot be silently left unpersisted by three of the four terminal paths.
_PERSISTED = (
    "status",
    "current_node_id",
    "variables",
    "blocks_since_pause",
    "wait_config",
    "last_error",
    "channel_connection",
    "updated_at",
)


class EngineError(RuntimeError):
    """Base for the engine's own refusals."""


class FlowNotRunnableError(EngineError):
    """There is nothing to run: no published version, or an unrunnable graph.

    Not an exceptional condition from the *caller's* point of view — a trigger
    can legitimately point at a flow somebody has since unpublished — which is
    why the queue handlers catch it and log instead of burning five retries on
    a state that will never change on its own.
    """


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def start_flow(
    contact: Any,
    flow: Flow,
    *,
    started_by: str,
    variables: dict[str, Any] | None = None,
    flow_version: FlowVersion | None = None,
    connection: Any = None,
    _carry_blocks: int = 0,
) -> FlowExecution:
    """Begin ``flow`` for ``contact`` and run it until it pauses or ends.

    ``flow_version`` names an explicit version, which is how #12's "test on
    Telegram" runs a **draft** without the runner needing a preview mode: the
    resulting execution is flagged ``preview`` and L7-A's counters exclude it.
    Omitted, the flow's published version is used and a flow with none raises
    :class:`FlowNotRunnableError`.

    ``connection`` is the channel this run happens on. It is remembered on the
    execution because ROADMAP contract 1 requires one on every send and SPEC
    §9.3 routes inbound events to the "waiting execution on that channel".

    **Supersedes.** SPEC §9.2 and §22: exactly one live execution per contact,
    across every flow. Any live execution the contact already has is expired
    here, under the lock, together with the queue rows that would have resumed
    it.
    """
    if contact.workspace_id != flow.workspace_id:
        raise WorkspaceMismatchError("That flow belongs to a different workspace than the contact.")
    if connection is not None and connection.workspace_id != flow.workspace_id:
        # `ContactScopedModel` checks the execution's contact against its
        # `peer_field` (the flow) and nothing else, so the connection FK is not
        # covered by any model-level guard. Storing a foreign one would hand
        # this workspace's contact to another tenant's channel on the first
        # send — the connection goes to `send_outbound` verbatim.
        raise WorkspaceMismatchError("That channel connection belongs to a different workspace than the flow.")

    version = _resolve_version(flow, flow_version)
    graph = Graph(version.graph_json)
    entry = graph.entry_node_id()
    if entry is None:
        raise FlowNotRunnableError(
            f"Flow {flow.pk} version {version.version} has no single entry node, so there is nowhere "
            f"to start (SPEC §9.1). Publishing validates this; a draft preview does not."
        )

    with transaction.atomic(), contact_lock(contact):
        superseded = _supersede(contact)
        execution = FlowExecution(
            workspace=flow.workspace,
            flow=flow,
            flow_version=version,
            contact=contact,
            channel_connection=connection,
            status=ExecutionStatus.RUNNING,
            current_node_id=entry,
            variables=dict(variables or {}),
            blocks_since_pause=_carry_blocks,
            started_by=started_by,
            preview=not version.published,
        )
        execution.save()
        logger.info(
            "Flow execution %s started flow=%s version=%s contact=%s preview=%s superseded=%s",
            execution.pk,
            flow.pk,
            version.version,
            contact.pk,
            execution.preview,
            superseded,
        )
        return _run(execution, graph)


def resume_execution(
    execution: FlowExecution,
    *,
    handle: str = "default",
    token: str | None = None,
) -> FlowExecution:
    """Follow ``handle`` out of the node this execution is waiting at, and run on.

    ``token`` is the wait nonce (:mod:`apps.flows.engine.waits`). Passing the one
    the scheduled action was created with is what makes a stale timer harmless:
    an execution that has since been superseded, resumed by a reply, or re-entered
    the same node carries a different token, so the old timer finds nothing to do
    and says so instead of jumping a live run to its ``timeout`` branch.

    Returns the execution unchanged when it is no longer resumable. That is a
    normal outcome, not an error — races between a reply and a timer are
    expected, and the loser must be a no-op rather than a retry.
    """
    with transaction.atomic(), contact_lock(execution.contact_id):
        current = locked_execution(execution)
        if current is None:
            logger.info("Resume skipped: execution %s no longer exists.", execution.pk)
            return execution
        if not current.is_live:
            logger.info("Resume skipped: execution %s is %s.", current.pk, current.status)
            return current
        if token is not None and current.wait_config.get("token") != token:
            logger.info(
                "Resume skipped: execution %s carries a different wait token, so this timer is stale.",
                current.pk,
            )
            return current
        return advance(current, Graph(current.flow_version.graph_json), handle)


def advance(execution: FlowExecution, graph: Graph, handle: str) -> FlowExecution:
    """Move off the current node through ``handle`` and re-enter the loop.

    The caller holds the transaction and the contact lock. Separated from
    :func:`resume_execution` because PR 2's ``attempt_resume`` has already done
    the loading, locking and matching by the time it knows which handle a
    contact's reply chose.
    """
    target = graph.target(execution.current_node_id, handle)
    if target is None:
        # SPEC §9.2: "Missing edge for a handle -> End". A question whose
        # `timeout` goes nowhere is a finished conversation, not a broken graph.
        logger.debug(
            "Execution %s: handle %r leaves node %s nowhere; completing.",
            execution.pk,
            handle,
            execution.current_node_id,
        )
        return _complete(execution)

    execution.current_node_id = target
    execution.status = ExecutionStatus.RUNNING
    execution.wait_config = {}
    return _run(execution, graph)


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


def _run(execution: FlowExecution, graph: Graph) -> FlowExecution:
    """Dispatch nodes until the execution pauses or reaches a terminal state."""
    while True:
        node_id = execution.current_node_id
        node = graph.node(node_id)
        if node is None:
            # Only reachable for a draft: a published version's graph is frozen,
            # but SPEC §16's autosave rewrites the draft under a running preview.
            return _fail(execution, f"node {node_id!r} is not in this flow version's graph")

        node_type = graph.node_type(node_id)
        node_class = node_class_for(node_type)
        if node_class is None:
            # A schema without a runtime: external_request until L4-E, send_sms
            # and send_email until L5-D/E. Publishing allows it, so the run has
            # to be the thing that says no.
            return _fail(execution, f"no runtime is registered for {node_type!r} nodes")

        ctx = NodeContext(
            execution=execution,
            graph=graph,
            node_id=node_id,
            node_type=node_type,
            config=graph.config(node_id),
            variables=execution.variables if isinstance(execution.variables, dict) else {},
        )
        result: StepResult = node_class().execute(ctx)
        execution.variables = ctx.variables

        if isinstance(result, Continue):
            execution.blocks_since_pause += 1
            if execution.blocks_since_pause >= LOOP_CAP:
                return _fail(execution, LOOP_CAP_ERROR, loop_cap=True)
            target = graph.target(node_id, result.handle)
            if target is None:
                return _complete(execution)
            execution.current_node_id = target
            continue

        if isinstance(result, Wait):
            return _wait(execution, result.config)

        if isinstance(result, Schedule):
            return _schedule(execution, result)

        if isinstance(result, Fail):
            return _fail(execution, result.error)

        if isinstance(result, End):
            if result.start_next is None:
                return _complete(execution)
            # SPEC §11.3's hand-off is a block like any other, and counting it
            # is what stops a ring of flows that are nothing but start_flow
            # nodes from recursing forever: no node in that ring ever returns
            # Continue, so nothing else would ever move the counter.
            execution.blocks_since_pause += 1
            if execution.blocks_since_pause >= LOOP_CAP:
                return _fail(execution, LOOP_CAP_ERROR, loop_cap=True)
            completed = _complete(execution)
            return _hand_off(completed, result.start_next)

        raise TypeError(  # pragma: no cover - a node returning something else
            f"{node_class.__name__}.execute returned {type(result).__name__}, which is not a StepResult."
        )


# ---------------------------------------------------------------------------
# State transitions. One function per outcome; nothing else writes the row.
# ---------------------------------------------------------------------------


def _persist(execution: FlowExecution) -> FlowExecution:
    execution.save(update_fields=list(_PERSISTED))
    return execution


def _tokenised(config: dict[str, Any]) -> dict[str, Any]:
    """A copy of ``config`` that definitely carries a wait token.

    The token is what makes a scheduled wake-up safe to ignore: a timer that
    fires for a wait the contact already answered, or for an execution that has
    since been superseded, finds a different token and does nothing. Minting one
    here rather than trusting each node means the guarantee holds for every
    pause, including one a later layer's node forgets to tokenise — and it means
    the idempotency key built from it is unique per wait rather than per node,
    so a node parked twice does not silently reuse the first wake-up's row.
    """
    tokenised = dict(config)
    if not tokenised.get("token"):
        tokenised["token"] = uuid7().hex
    return tokenised


def _wait(execution: FlowExecution, config: dict[str, Any]) -> FlowExecution:
    """SPEC §9.3: park until an inbound event matches, resetting the loop cap."""
    execution.status = ExecutionStatus.WAITING_REPLY
    execution.wait_config = _tokenised(config)
    execution.blocks_since_pause = 0
    _persist(execution)
    _schedule_wait_timer(execution)
    logger.debug("Execution %s waiting at node %s", execution.pk, execution.current_node_id)
    return execution


def _schedule(execution: FlowExecution, result: Schedule) -> FlowExecution:
    """SPEC §9.3: park until a moment, resumable only by the action scheduled here."""
    config = _tokenised(result.config)
    config.setdefault("type", "smart_delay")
    config["handle"] = result.resume_handle
    execution.status = ExecutionStatus.WAITING_DELAY
    execution.wait_config = config
    execution.blocks_since_pause = 0
    _persist(execution)

    token = str(config["token"])
    schedule(
        ActionType.RESUME_EXECUTION,
        result.run_at,
        {"execution_id": str(execution.pk), "handle": result.resume_handle, "token": token},
        workspace=execution.workspace,
        contact=execution.contact_id,
        idempotency_key=f"exec:{execution.pk}:node:{execution.current_node_id}:wait:{token}",
    )
    logger.debug("Execution %s sleeping until %s", execution.pk, result.run_at)
    return execution


def _schedule_wait_timer(execution: FlowExecution) -> None:
    """Arm the followup timer a wait asked for (SPEC §11.1's ``followup``).

    The wait config owns the decision; the runner owns the queue row, so a node
    never has to know how work is scheduled. ``run_at`` absent means the wait
    has no deadline and simply waits.
    """
    timeout = execution.wait_config.get("timeout")
    if not isinstance(timeout, dict):
        return
    run_at = timeout.get("run_at")
    if not run_at:
        return
    token = str(execution.wait_config["token"])
    schedule(
        ActionType.FOLLOWUP_TIMER,
        _as_datetime(run_at),
        {
            "execution_id": str(execution.pk),
            "handle": timeout.get("handle") or "timeout",
            "token": token,
        },
        workspace=execution.workspace,
        contact=execution.contact_id,
        idempotency_key=f"exec:{execution.pk}:node:{execution.current_node_id}:timeout:{token}",
    )


def _complete(execution: FlowExecution) -> FlowExecution:
    """Terminal, successfully. Emits contract 7's ``execution.completed``.

    ``current_node_id`` is left pointing at the node the run ended on rather
    than blanked: "where did this stop" is the first question anyone asks of a
    finished execution, and ``status`` already says it is not going anywhere.
    """
    execution.status = ExecutionStatus.COMPLETED
    execution.wait_config = {}
    _persist(execution)
    events.emit(
        events.EVENT_EXECUTION_COMPLETED,
        workspace_id=execution.workspace_id,
        contact_id=execution.contact_id,
        execution_id=execution.pk,
        flow_id=execution.flow_id,
        flow_version_id=execution.flow_version_id,
        preview=execution.preview,
    )
    logger.info("Flow execution %s completed at node %s", execution.pk, execution.current_node_id)
    return execution


def _fail(execution: FlowExecution, error: str, *, loop_cap: bool = False) -> FlowExecution:
    """Terminal, broken. Stores the reason and tells the workspace's admins.

    Scrubbed and capped on the way into the column for the same reasons
    ``apps.queueing.worker`` does it: this text is rendered in the admin, and an
    error string routinely quotes the value that caused it.
    """
    execution.status = ExecutionStatus.FAILED
    execution.wait_config = {}
    execution.last_error = _storable(error)
    _persist(execution)
    logger.error(
        "Flow execution %s failed at node %s: %s",
        execution.pk,
        execution.current_node_id,
        execution.last_error,
    )
    _notify_failure(execution, loop_cap=loop_cap)
    return execution


def _notify_failure(execution: FlowExecution, *, loop_cap: bool) -> None:
    """Raise the in-app alert whose copy is already registered for this case.

    Both event types exist in ``apps/notifications/events.py`` naming this
    consumer; the copy is registered data, so nothing here builds a sentence.
    Notification failures must not take the flow engine with them — a broken
    mail backend is not a reason to lose the record that the run failed.

    **The savepoint is what makes that true.** ``notify()`` writes rows, and a
    database error inside an ``atomic()`` block marks the whole transaction
    rollback-only; catching it here would not clear that flag, so the caller's
    ``atomic()`` would roll back on exit and take the ``failed`` status this
    function was called to announce — plus every node write before it — with it.
    Running the notification in a nested ``atomic()`` and catching *outside* it
    releases to the savepoint instead, which is the one arrangement where
    "non-fatal" is actually non-fatal.
    """
    from apps.notifications.engine import notify

    context = {
        "flow_name": execution.flow.name,
        "contact_name": execution.contact.display_name,
        "node_label": execution.current_node_id or "the entry node",
        "error": execution.last_error,
    }
    try:
        with transaction.atomic():
            notify(
                execution.workspace,
                "flow_loop_cap_hit" if loop_cap else "flow_execution_failed",
                roles=("admin",),
                context=context,
            )
    except Exception:  # noqa: BLE001 - see the docstring
        logger.exception("Could not notify admins that execution %s failed", execution.pk)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_version(flow: Flow, flow_version: FlowVersion | None) -> FlowVersion:
    """Which version to run, with the reasons a flow may not be runnable."""
    from apps.flows.models import FlowStatus

    if flow.status == FlowStatus.ARCHIVED:
        raise FlowNotRunnableError(f"Flow {flow.pk} is archived.")

    if flow_version is not None:
        if flow_version.flow_id != flow.pk:
            raise ValueError("That flow version belongs to a different flow.")
        return flow_version

    version = published_version(flow)
    if version is None:
        raise FlowNotRunnableError(f"Flow {flow.pk} has no published version.")
    return version


def _supersede(contact: Any) -> int:
    """Expire every live execution this contact has, and disarm their timers.

    SPEC §22's decision, written out: "One live execution per contact across all
    flows; new start supersedes." Doing it here, under the lock the new
    execution is about to be created under, is what makes it atomic — the
    database's partial unique index only covers (contact, flow), so the
    cross-flow half of the rule is this function.

    The queue rows go too. A superseded execution's followup timer would
    otherwise fire hours later; :func:`resume_execution`'s status check would
    catch it, but a cancelled row is one that never wakes a worker at all.
    """
    live = FlowExecution.objects.for_workspace(contact.workspace_id).filter(
        contact=contact, status__in=sorted(LIVE_STATUSES)
    )
    expired = live.update(status=ExecutionStatus.EXPIRED, wait_config={}, updated_at=timezone.now())
    if not expired:
        return 0

    # Only the two types that resume an execution. A pending `start_flow` row is
    # a future run somebody scheduled on purpose and is none of this function's
    # business.
    ScheduledAction.objects.for_workspace(contact.workspace_id).filter(
        contact_id=contact.pk,
        status=ActionStatus.PENDING,
        type__in=(ActionType.RESUME_EXECUTION, ActionType.FOLLOWUP_TIMER),
    ).update(status=ActionStatus.CANCELLED, updated_at=timezone.now())
    return expired


def _hand_off(execution: FlowExecution, start_next: StartNext) -> FlowExecution:
    """SPEC §11.3: start the target flow under the same lock, counter carried.

    A target that cannot be resolved is a warning rather than a failure, and
    that is a deliberate reading: this execution has already run its graph to
    the end and been recorded as completed. Retroactively calling it failed
    because the *next* flow is missing would misreport what happened, and the
    author's real problem — a start_flow node pointing at a deleted flow — is
    better served by a log line than by a run that looks broken.
    """
    flow = _target_flow(execution, start_next.flow_id)
    if flow is None:
        return execution
    try:
        return start_flow(
            execution.contact,
            flow,
            started_by=StartedBy.stamp(StartedBy.FLOW, execution.pk),
            # Carried, not dropped: an author who collects an email in one flow
            # and chains to another expects the email to still be there, and
            # silently losing collected data is the worse surprise.
            variables=dict(execution.variables or {}),
            connection=execution.channel_connection,
            _carry_blocks=execution.blocks_since_pause,
        )
    except (FlowNotRunnableError, WorkspaceMismatchError) as exc:
        logger.warning("Execution %s could not hand off to flow %s: %s", execution.pk, flow.pk, exc)
        return execution


def _target_flow(execution: FlowExecution, flow_id: str) -> Flow | None:
    """Resolve a start_flow node's ``flow_id``, scoped, or ``None`` with a warning."""
    try:
        pk = UUID(str(flow_id))
    except (ValueError, AttributeError, TypeError):
        logger.warning("Execution %s: start_flow node names %r, which is not a flow id.", execution.pk, flow_id)
        return None
    flow = Flow.objects.for_workspace(execution.workspace_id).filter(pk=pk).first()
    if flow is None:
        logger.warning("Execution %s: start_flow node names flow %s, which does not exist here.", execution.pk, pk)
    return flow


def locked_execution(execution: FlowExecution) -> FlowExecution | None:
    """Re-read one execution inside the lock, with everything a step needs.

    Re-read rather than trusted: the caller's instance may have been loaded
    before the lock was taken, and between those two moments another worker may
    have completed, expired or superseded it.

    ``of=("self",)`` is load-bearing twice over. It keeps the row lock on the
    execution instead of also locking the flow, the version, the contact and the
    connection — rows other work legitimately touches, and a wider lock is a
    wider deadlock surface. And ``channel_connection`` is nullable, so its
    ``select_related`` is a LEFT OUTER JOIN: Postgres refuses ``FOR UPDATE`` on
    the nullable side of one outright.

    ``workspace__organization`` is in the list because ``NodeContext.workspace``
    is read by four node paths (media resolution, tag creation, notify_members,
    the smart-delay clock) and ``Workspace.effective_timezone`` reaches through
    to the organization — so leaving them out cost two lazy queries on the
    critical path of every resumed step.
    """
    return (
        FlowExecution.objects.for_workspace(execution.workspace_id)
        .select_for_update(of=("self",))
        .select_related("flow_version", "contact", "flow", "channel_connection", "workspace", "workspace__organization")
        .filter(pk=execution.pk)
        .first()
    )


def _as_datetime(value: Any) -> datetime:
    """Accept an ISO string (as stored in ``wait_config``) or a datetime."""
    if isinstance(value, datetime):
        return value
    parsed = datetime.fromisoformat(str(value))
    if timezone.is_naive(parsed):  # pragma: no cover - wait configs always store aware instants
        parsed = timezone.make_aware(parsed)
    return parsed


def _storable(text: str) -> str:
    text = scrub(str(text))
    if len(text) > MAX_STORED_ERROR_CHARS:
        text = text[: MAX_STORED_ERROR_CHARS - 1] + "…"
    return text
