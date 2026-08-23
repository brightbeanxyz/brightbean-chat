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
from apps.flows.models import FlowExecution, FlowVersion, TriggerType
from apps.flows.triggers.hooks import Stage
from apps.flows.triggers.stages import waiting_execution_for

__all__ = ["first_step_is_safe", "resume_first_step_is_safe", "trigger_first_step_is_safe"]

logger = logging.getLogger(__name__)


def first_step_is_safe(context: Any, stage: Stage) -> bool:
    """Whether the stage about to run could complete inside the request.

    Conservative by construction: an unknown shape answers ``False``, and
    ``False`` only ever means "let the worker do it", which is always correct.
    """
    if stage is Stage.RESUME:
        execution = waiting_execution_for(context)
        if execution is not None:
            return resume_first_step_is_safe(execution)
        # Nothing is waiting, so this event will fall through to the trigger
        # stage in the same pass. Judge it on what that would do.
    return trigger_first_step_is_safe(context)


def trigger_first_step_is_safe(context: Any) -> bool:
    """Whether every flow a trigger could start here begins with a safe node.

    It asks about *candidates* rather than re-running the match, because this
    runs before the lock is taken and a match is not free. Requiring **all**
    candidates to be safe rather than the one that will win is the conservative
    direction: the worst outcome is a flow that could have replied in-request
    being answered a second later by the worker.
    """
    from apps.flows.triggers.matching import EVENT_TRIGGER_TYPES, MatchContext, eligible_triggers
    from apps.flows.triggers.stages import DEFAULT_REPLY_EVENTS

    match_context = MatchContext.from_event(context.connection, context.event, contact=context.contact)
    types = EVENT_TRIGGER_TYPES.get(context.event.type, ())
    if context.contact is not None and context.event.type in DEFAULT_REPLY_EVENTS:
        # The default reply is a stage rather than a candidate, but it is one
        # more flow this pass could start — so it is judged in the same query
        # rather than in a second one.
        types = (*types, TriggerType.DEFAULT_REPLY)

    flows = {trigger.flow_id: trigger.flow for trigger in eligible_triggers(match_context, types)}
    if not flows:
        # Nothing to start. Going inline means the stages run, find nothing and
        # return, which is exactly what should happen.
        return True
    return all(_entry_is_safe(graph) for graph in _published_graphs(flows.values()))


def _published_graphs(flows: Any) -> list[Graph]:
    """Every candidate's published graph, in **one** query rather than one each.

    ``eligible_triggers`` has already established that each of these flows has a
    published version — its candidate query filters on ``Exists(published)`` — so
    this is a fetch, not a check, and fetching them one flow at a time would put
    an N+1 on the path SPEC §7.1 budgets at 1.5 seconds.
    """
    flows = list(flows)
    if not flows:
        return []
    versions = FlowVersion.objects.for_workspace(flows[0].workspace_id).filter(flow__in=flows, published=True)
    return [Graph(version.graph_json) for version in versions]


def _entry_is_safe(graph: Graph) -> bool:
    entry = graph.entry_node_id()
    if entry is None:
        # An empty graph starts nothing, so it cannot start anything unsafe.
        return True
    return synchronous_safe(graph.node_type(entry))


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
