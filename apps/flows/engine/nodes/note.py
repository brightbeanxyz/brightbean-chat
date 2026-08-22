"""SPEC §11.11 — the builder's sticky note, and why it still has a runtime.

    Config: text. Ignored at runtime, builder-only annotation.

A note is genuinely unreachable: ``NodeSpec.annotation`` keeps it out of
entry-node detection, and validation rejects any edge that touches one, so no
published graph can route to it. The class exists anyway so the runtime registry
is *total* over the node types the schema describes — the alternative is a
registry with a hole in it, and a hole is indistinguishable from a node type
somebody forgot to implement.
"""

from apps.flows.engine.context import NodeContext
from apps.flows.engine.nodes.base import Node
from apps.flows.engine.registry import register_node
from apps.flows.engine.results import End, StepResult

__all__ = ["NoteNode"]


@register_node
class NoteNode(Node):
    """Does nothing, costs nothing, and cannot be reached."""

    type = "note"
    synchronous_safe = True

    def execute(self, ctx: NodeContext) -> StepResult:
        return End()
