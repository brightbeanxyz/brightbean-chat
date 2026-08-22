"""SPEC §11.6 — a weighted split that remembers which way it sent someone.

    Config: paths[] {id, weight percent}, sticky bool (default true). Sticky:
    first pass stores path in variables under ``rand:<node_id>`` and reuses it.
    Handles: rand:<id>.

Sticky is the default and SPEC §22 confirms it ("Randomizer sticky by default,
per-node toggle"). It is what makes an A/B test mean anything: a contact who
loops back through the same node — a retry, a re-triggered flow — has to stay in
the arm they were assigned to, or the two arms stop being comparable populations
and start being a coin flipped per message.

The variable key is SPEC's, ``rand:<node_id>``, and it deliberately collides
with nothing: node ids cannot contain a colon (see
:mod:`apps.flows.schema.handles`), so no author-chosen variable name can shadow
one.

**Why ``secrets`` and not ``random``.** Nothing here is a security boundary, but
ruff's ``S311`` flags ``random`` for exactly this shape of call and the honest
options are a ``# noqa`` on a coin flip or the stdlib function that needs none.
``secrets.randbelow`` is the same one line, so there is no trade to make.
"""

import logging
import secrets
from typing import Any

from apps.flows.engine.context import NodeContext
from apps.flows.engine.nodes.base import Node
from apps.flows.engine.registry import register_node
from apps.flows.engine.results import Continue, Fail, StepResult

__all__ = ["RandomizerNode", "variable_key"]

logger = logging.getLogger(__name__)


def variable_key(node_id: str) -> str:
    """SPEC §11.6's sticky slot for one randomizer node."""
    return f"rand:{node_id}"


@register_node
class RandomizerNode(Node):
    """Weighted split, sticky per contact per node."""

    type = "randomizer"
    synchronous_safe = True

    def execute(self, ctx: NodeContext) -> StepResult:
        paths = [path for path in (ctx.config.get("paths") or []) if isinstance(path, dict)]
        ids = [path["id"] for path in paths if isinstance(path.get("id"), str)]
        if not ids:
            return Fail(f"randomizer node {ctx.node_id} has no paths")

        key = variable_key(ctx.node_id)
        sticky = ctx.config.get("sticky", True)
        if sticky:
            remembered = ctx.variables.get(key)
            if isinstance(remembered, str) and remembered in ids:
                logger.debug("Execution %s: randomizer %s reusing %s", ctx.execution.pk, ctx.node_id, remembered)
                return Continue(f"rand:{remembered}")

        chosen = _weighted_choice(paths, ids)
        if sticky:
            ctx.set_variable(key, chosen)
        logger.debug("Execution %s: randomizer %s chose %s", ctx.execution.pk, ctx.node_id, chosen)
        return Continue(f"rand:{chosen}")


def _weighted_choice(paths: list[dict[str, Any]], ids: list[str]) -> str:
    """Pick a path id, weight-proportionally.

    Weights are percentages the builder is expected to keep summing to 100, but
    nothing enforces that and nothing needs to: sampling below the *actual*
    total is correct for any set of non-negative weights, so a graph adding up
    to 90 or 130 still splits in the ratio its author drew.

    All-zero weights fall back to a uniform pick. The alternative — refusing to
    route — would strand a flow on a state the builder can produce with two
    clicks, and "every path is impossible" has no reading other than "the author
    has not set the weights yet".
    """
    weights = [max(0, int(path.get("weight") or 0)) for path in paths if isinstance(path.get("id"), str)]
    total = sum(weights)
    if total <= 0:
        return ids[secrets.randbelow(len(ids))]

    ticket = secrets.randbelow(total)
    for path_id, weight in zip(ids, weights, strict=True):
        if ticket < weight:
            return path_id
        ticket -= weight
    return ids[-1]  # pragma: no cover - unreachable while total == sum(weights)
