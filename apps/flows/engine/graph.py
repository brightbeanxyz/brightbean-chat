"""Reading a published graph — node lookup, edge following, entry point.

``flow_version.graph_json`` is a document (SPEC §9.1), and the runner asks it
three questions over and over: which node is ``current_node_id``, where does
handle *h* leave node *n*, and where does a fresh run start. Doing that against
the raw lists means an O(nodes) scan per step; a 200-node flow with a 30-block
budget would walk 6,000 dicts to run once.

So the graph is indexed once per execution and the runner holds the index. That
is the only reason this class exists — it adds no rules of its own. In
particular **the entry rule is not restated here**: it comes from
:func:`apps.flows.schema.validation.entry_node_id`, the same function the
publish gate uses, because a validator and a runner that disagree about where a
flow starts would publish cleanly and then run the wrong node.

Everything is read defensively. A published graph has been through validation,
but a *draft* graph has not — SPEC §16 lets the builder autosave a half-wired
flow every two seconds, and #12's preview runs exactly that draft on a real
channel. So a missing key answers ``None`` rather than raising.
"""

from typing import Any

from apps.flows.schema import entry_node_id

__all__ = ["Graph"]


class Graph:
    """An indexed view over one ``graph_json`` document."""

    __slots__ = ("_edges", "_nodes", "_raw")

    def __init__(self, graph: Any) -> None:
        self._raw: dict[str, Any] = graph if isinstance(graph, dict) else {}
        self._nodes: dict[str, dict[str, Any]] = {}
        for node in self._raw.get("nodes") or []:
            if isinstance(node, dict) and isinstance(node.get("id"), str):
                self._nodes[node["id"]] = node

        # (source, handle) -> target. Validation already rejects two edges
        # leaving one handle, so the first-one-wins collapse `setdefault` gives
        # can only bite a draft — and picking one deterministically beats a
        # runner that raises while the author is still wiring the node up.
        self._edges: dict[tuple[str, str], str] = {}
        for edge in self._raw.get("edges") or []:
            if not isinstance(edge, dict):
                continue
            source, handle, target = edge.get("source"), edge.get("sourceHandle"), edge.get("target")
            if isinstance(source, str) and isinstance(handle, str) and isinstance(target, str):
                self._edges.setdefault((source, handle), target)

    def node(self, node_id: str | None) -> dict[str, Any] | None:
        """The node with this id, or ``None``."""
        if not node_id:
            return None
        return self._nodes.get(node_id)

    def node_type(self, node_id: str | None) -> str:
        node = self.node(node_id)
        node_type = node.get("type") if node else None
        return node_type if isinstance(node_type, str) else ""

    def config(self, node_id: str | None) -> dict[str, Any]:
        """A node's config, always a dict.

        The condition node's config is a filter document rather than an object
        with named keys, and every other node's is an object; both are dicts, so
        callers never have to check.
        """
        node = self.node(node_id)
        config = node.get("config") if node else None
        return config if isinstance(config, dict) else {}

    def target(self, node_id: str, handle: str) -> str | None:
        """Where ``handle`` leaves ``node_id``, or ``None`` — SPEC §9.2's End."""
        return self._edges.get((node_id, handle))

    def entry_node_id(self) -> str | None:
        """Where a fresh run of this graph starts (SPEC §9.1).

        ``None`` when the graph has zero or several entries, which validation
        reports as a graph error and refuses to publish.
        """
        return entry_node_id(self._raw)
