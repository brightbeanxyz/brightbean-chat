"""``NodeContext`` — everything a node is allowed to see, and nothing more.

A node gets one argument. That is a deliberate narrowing: a node that reached
for ``execution.save()`` or opened its own transaction would be writing state the
runner is in the middle of managing, and the resulting "who wrote status last"
question is exactly the bug the five-way :mod:`~apps.flows.engine.results`
vocabulary exists to prevent. So the context hands out reads freely and offers
exactly one write — :meth:`set_variable`, which mutates the in-memory
``variables`` dict that the runner persists at the next pause or terminal state.

Two conveniences earn their place because otherwise every node would repeat
them: :meth:`render` (nodes must not build their own renderer — SECURITY-BASELINE
§3) and :attr:`render_context`, which is built lazily and **once per node
execution**, so a ``send_message`` with ten blocks runs one custom-field query
rather than ten.

The context is created fresh for each node the runner dispatches. It is not a
place to stash state between nodes; ``variables`` is.
"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from apps.flows.rendering import RenderContext, context_for, render, render_json

if TYPE_CHECKING:
    from apps.flows.engine.graph import Graph
    from apps.flows.models import FlowExecution

__all__ = ["NodeContext"]


@dataclass
class NodeContext:
    """One node's view of the run it is part of."""

    execution: "FlowExecution"
    graph: "Graph"
    node_id: str
    node_type: str
    config: dict[str, Any]
    #: The live variable bag. Mutating it here is how a node passes a value on;
    #: the runner writes it back to the row at the next pause or terminal state.
    variables: dict[str, Any]

    _render_context: RenderContext | None = field(default=None, repr=False, compare=False)

    @property
    def contact(self) -> Any:
        return self.execution.contact

    @property
    def workspace(self) -> Any:
        return self.execution.workspace

    @property
    def workspace_id(self) -> Any:
        return self.execution.workspace_id

    @property
    def connection(self) -> Any:
        """The channel this run is happening on, or ``None``.

        ``None`` for a run started by the API or a rule trigger that has not
        touched a channel. A node that needs one — anything that sends — has to
        say so rather than assume.
        """
        return self.execution.channel_connection

    @property
    def render_context(self) -> RenderContext:
        """The placeholder context, built once and cached for this node."""
        if self._render_context is None:
            self._render_context = context_for(self.contact, self.variables)
        return self._render_context

    def render(self, template: Any, *, mode: str = "text") -> str:
        """Substitute placeholders in author text (SECURITY-BASELINE §3).

        The only rendering entry point a node may use. Nodes do not import
        :mod:`apps.flows.rendering` directly and never touch a template engine;
        routing every node through one method is what makes "is any user content
        evaluated anywhere?" a question with one place to look.
        """
        return render(template, self.render_context, mode=mode)

    def render_json(self, template: Any, *, mode: str = "text") -> Any:
        """Substitute placeholders throughout a JSON document, structure kept.

        :meth:`render`'s rule applied to node config that is a *document* rather
        than a sentence — SPEC §11.7's External Request body. Offered here, and
        not imported from :mod:`apps.flows.rendering` by the node, for the same
        reason :meth:`render` is: one method per node-visible rendering entry
        point is what keeps "is any user content evaluated anywhere?" a question
        with one place to look.
        """
        return render_json(template, self.render_context, mode=mode)

    def set_variable(self, key: str, value: Any) -> None:
        """Record a value for later nodes and for ``{{placeholders}}``.

        Invalidates the cached render context, because a node that writes a
        variable and then renders with it — data collection does exactly that —
        must see its own write.
        """
        self.variables[str(key)] = value
        self._render_context = None
