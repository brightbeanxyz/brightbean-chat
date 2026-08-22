"""SPEC §11.4 — branch on a contact filter, using ROADMAP contract 8's engine.

    Config: ``{ match: all|any, rules: [...] }``. Handles: cond:true, cond:false.

The whole node is one call to :func:`apps.contacts.conditions.evaluate`. That is
the point of contract 8, and the ROADMAP says so in as many words: "L3-B's
condition node calls ``evaluate()`` — no re-implementation anywhere." The
operator table, the absence semantics ("everyone not tagged VIP" includes people
who have never been tagged), the day boundaries and the workspace timezone all
live in that module, and a second reading of them here would eventually disagree
with the segment the same filter defines — a contact receiving a message their
segment says they will not receive.

The node's ``config`` *is* the filter document; ``NodeSpec.config`` is
``$ref: condition_filter``, not an object wrapping one.
"""

import logging

from apps.contacts.conditions import ConditionError, evaluate
from apps.flows.engine.context import NodeContext
from apps.flows.engine.nodes.base import Node
from apps.flows.engine.registry import register_node
from apps.flows.engine.results import Continue, Fail, StepResult

__all__ = ["ConditionNode"]

logger = logging.getLogger(__name__)


@register_node
class ConditionNode(Node):
    """True/false split on the contact's current state."""

    type = "condition"
    synchronous_safe = True

    def execute(self, ctx: NodeContext) -> StepResult:
        try:
            matched = evaluate(ctx.contact, ctx.config)
        except ConditionError as exc:
            # Diagnosable and permanent: the filter names a tag or field that
            # has been deleted, or a source whose owner has not landed
            # (SourceNotEvaluableError). Retrying cannot help, so this is a Fail
            # rather than an exception left to the queue's backoff ladder.
            return Fail(f"condition node {ctx.node_id}: {exc}")

        handle = "cond:true" if matched else "cond:false"
        logger.debug("Execution %s: condition %s -> %s", ctx.execution.pk, ctx.node_id, handle)
        return Continue(handle)
