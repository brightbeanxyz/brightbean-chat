"""SPEC §11.3 — end this run and begin another, under the same lock.

    Config: flow_id. Behavior: terminates current execution (completed), starts
    target flow's published version immediately under the same lock. Handle:
    none (terminal in-graph).

The node itself only *names* the target. Ending the current execution, emitting
``execution.completed`` and starting the next one are the runner's writes, in
that order, and it carries ``blocks_since_pause`` across the hand-off so a ring
of flows that call each other still hits the loop cap. See
:class:`apps.flows.engine.results.End` for why the hand-off is a field on ``End``
rather than a sixth ``StepResult``.
"""

from apps.flows.engine.context import NodeContext
from apps.flows.engine.nodes.base import Node
from apps.flows.engine.registry import register_node
from apps.flows.engine.results import End, Fail, StartNext, StepResult

__all__ = ["StartFlowNode"]


@register_node
class StartFlowNode(Node):
    """Terminal in-graph: hands the contact to another flow."""

    type = "start_flow"
    synchronous_safe = True

    def execute(self, ctx: NodeContext) -> StepResult:
        flow_id = ctx.config.get("flow_id")
        if not isinstance(flow_id, str) or not flow_id:
            # Required by the schema, so only a hand-edited or draft graph gets
            # here — and "start nothing" would silently look like a normal end.
            return Fail(f"start_flow node {ctx.node_id} names no flow")
        return End(start_next=StartNext(flow_id=flow_id))
