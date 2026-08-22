"""The node interface: ``execute(ctx) -> StepResult`` and one class attribute.

SPEC §9.2 gives the whole contract in a sentence — "Node classes implement
``execute(ctx) -> StepResult``" — and the base class deliberately adds almost
nothing to it. Nodes are stateless: one instance is built per dispatch and
thrown away, so there is no ``__init__`` to get wrong and nothing that could
leak between two contacts' runs.

``synchronous_safe`` is the one addition, and it belongs on the class rather
than in a list somewhere because the question it answers is about the node's
own cost. SPEC §7.1 sets the rule:

    If a waiting execution or trigger matches AND the resulting first step is
    synchronous-safe (send message, action, condition, randomizer, start flow),
    execute inline under a total budget of 1.5 s wall clock […] Budget exceeded,
    node is not synchronous-safe, or any error: enqueue.

L4-A reads it through :func:`apps.flows.engine.registry.synchronous_safe`. The
default is ``False``: a node type added later is enqueued until somebody has
thought about whether it can finish inside a web request, which is the safe
direction to be wrong in.
"""

from abc import ABC, abstractmethod
from typing import ClassVar

from apps.flows.engine.context import NodeContext
from apps.flows.engine.results import StepResult

__all__ = ["Node"]


class Node(ABC):
    """One node type's runtime."""

    #: The graph ``type`` this class runs. Must have a ``NodeSpec``.
    type: ClassVar[str] = ""

    #: May this node run inline in the webhook request (SPEC §7.1)?
    synchronous_safe: ClassVar[bool] = False

    @abstractmethod
    def execute(self, ctx: NodeContext) -> StepResult:
        """Do this node's work and report what happened.

        Must not write ``execution.status``, open a transaction, or take a lock:
        the runner is inside both already (SPEC §9.6), and it owns every write
        to the execution row. Say what happened; the runner records it.
        """
