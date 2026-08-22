"""Is the first thing this event would do safe to do inside the request?

SPEC §7.1 names the condition — "the resulting first step is synchronous-safe" —
and :func:`apps.flows.engine.registry.synchronous_safe` is the answer, whose own
docstring says it is read by this budget. Each node class carries the verdict as
a class attribute, so there is deliberately **no second list of safe types
anywhere in this module**.

"The resulting first step" means two different nodes depending on the stage,
which is why this is a function and not an expression.
"""

import logging
from typing import Any

from apps.flows.engine import Graph, synchronous_safe
from apps.flows.models import ExecutionStatus, FlowExecution, FlowVersion, Trigger
from apps.flows.triggers.hooks import Stage

__all__ = ["first_step_is_safe", "resume_first_step_is_safe", "trigger_first_step_is_safe"]

logger = logging.getLogger(__name__)


def first_step_is_safe(context: Any, stage: Stage) -> bool:
    """Whether the stage about to run could complete inside the request.

    Conservative by construction: an unknown shape answers ``False``, and
    ``False`` only ever means "let the worker do it", which is always correct.
    """
    if stage is Stage.RESUME:
        execution = _waiting_execution(context)
        if execution is not None:
            return resume_first_step_is_safe(execution)
        # Nothing is waiting, so this event will fall through to the trigger
        # stage in the same pass. Judge it on what that would do.
    return trigger_first_step_is_safe(context)


def trigger_first_step_is_safe(context: Any) -> bool:
    """Whether every flow a trigger could start here begins with a safe node.

    It asks about *candidates* rather than re-running the match, because the
    match has side-effect-free matchers but a real cost, and this runs before the
    lock is taken. Requiring **all** candidates to be safe rather than the one
    that will win is the conservative direction: the worst outcome is a flow that
    could have replied in-request being answered a second later by the worker.
    """
    from apps.flows.triggers.matching import EVENT_TRIGGER_TYPES, candidates
    from apps.flows.triggers.matching import MatchContext as _MatchContext

    types = EVENT_TRIGGER_TYPES.get(context.event.type, ())
    match_context = _MatchContext.from_event(context.connection, context.event, contact=context.contact)
    queryset = candidates(match_context, types) if types else Trigger.objects.none()
    flows = [trigger.flow for trigger in queryset]

    default = _default_reply_flow(context)
    if default is not None:
        flows.append(default)

    if not flows:
        # Nothing to start. Cheap and safe, and going inline means the stages
        # run, find nothing, and return — which is what should happen.
        return True
    return all(_flow_entry_is_safe(flow) for flow in flows)


def resume_first_step_is_safe(execution: FlowExecution) -> bool:
    """Whether every branch out of the node this execution is parked at is safe.

    Deliberately does **not** work out which handle the event will choose. That
    would mean a second copy of ``apps.flows.engine.waits``'s answer-matching,
    and two copies disagreeing is precisely how a button press ends up on the
    wrong branch. Walking every outgoing edge instead costs one conservative
    answer — a ``smart_delay`` on one branch of five sends the whole thing to the
    worker — and needs no change to the engine.

    A node with no outgoing edges ends the flow, which is safe.
    """
    version = execution.flow_version
    graph = Graph(version.graph_json)
    node_id = execution.current_node_id
    if not node_id:
        return True
    targets = _outgoing_targets(version, node_id)
    return all(synchronous_safe(graph.node_type(target)) for target in targets)


def _outgoing_targets(version: FlowVersion, node_id: str) -> list[str]:
    edges = version.graph_json.get("edges") or []
    return [
        str(edge.get("target"))
        for edge in edges
        if isinstance(edge, dict) and edge.get("source") == node_id and edge.get("target")
    ]


def _flow_entry_is_safe(flow: Any) -> bool:
    version = _published_version(flow)
    if version is None:
        # Cannot start at all, so it cannot start anything unsafe. The candidate
        # query already excludes these; this is the belt to that braces.
        return True
    graph = Graph(version.graph_json)
    entry = graph.entry_node_id()
    if entry is None:
        return True
    return synchronous_safe(graph.node_type(entry))


def _published_version(flow: Any) -> FlowVersion | None:
    return FlowVersion.objects.for_workspace(flow.workspace_id).filter(flow=flow, published=True).first()


def _default_reply_flow(context: Any) -> Any | None:
    from apps.flows.triggers.stages import DEFAULT_REPLY_EVENTS, default_reply_trigger_for

    if context.event.type not in DEFAULT_REPLY_EVENTS or context.contact is None:
        return None
    trigger = default_reply_trigger_for(context)
    return trigger.flow if trigger is not None else None


def _waiting_execution(context: Any) -> FlowExecution | None:
    if context.contact is None:
        return None
    return (
        FlowExecution.objects.for_workspace(context.connection.workspace_id)
        .filter(
            contact=context.contact,
            status=ExecutionStatus.WAITING_REPLY,
            channel_connection=context.connection,
        )
        .select_related("flow_version")
        .order_by("-updated_at")
        .first()
    )
