"""The graph envelope (SPEC §9.1) and the input limits that guard it.

The persisted shape is exactly SPEC §9.1's::

    {"schema": 1,
     "nodes": [{"id": "n1", "type": "send_message", "position": {"x": 0, "y": 0}, "config": {...}}],
     "edges": [{"id": "e1", "source": "n1", "sourceHandle": "default", "target": "n2"}]}

and nothing else. React Flow decorates its own nodes with ``width``, ``selected``,
``dragging`` and friends; those are view state, and the builder serialises them
away before saving. Accepting them "harmlessly" is how a store that rejects
unknown keys stops rejecting unknown keys, and unknown-key rejection is the
mass-assignment guard SECURITY-BASELINE §7 asks for.

**Limits come first.** ``graph_json`` is a user-authored JSON document, so §7
requires size and depth caps *and* schema validation. The order matters: a
40 MB or 5000-deep document must be refused before anything walks it, because
the walk is the expense. Depth is measured iteratively for the same reason — a
recursive measurement of a hostile document is itself the denial of service it
was added to prevent.

Findings from this module are ``stage="document"``: they refuse the write. A
graph that is *structurally* sound but not yet runnable — a dangling edge, no
entry node — is a perfectly ordinary draft and is handled in
:mod:`apps.flows.schema.validation` instead.
"""

import json
import re
from typing import Any

from apps.flows.schema.issues import Issue
from apps.flows.schema.jsonschema import validate_instance
from apps.flows.schema.nodes import all_defs, node_spec

__all__ = [
    "GRAPH_KEYS",
    "limits",
    "MAX_EDGES",
    "MAX_GRAPH_BYTES",
    "MAX_GRAPH_DEPTH",
    "MAX_NODES",
    "SCHEMA_VERSION",
    "empty_graph",
    "graph_byte_size",
    "json_depth",
    "validate_document",
]

#: The one graph format v1 speaks. A document announcing anything else is
#: rejected rather than guessed at: a future format will want a migration, and a
#: migration that never ran is worse than a refusal.
SCHEMA_VERSION = 1

#: Serialized size ceiling. 512 KiB is roughly two orders of magnitude above the
#: largest realistic flow and still small enough that parsing one is cheap.
MAX_GRAPH_BYTES = 512 * 1024

#: Nesting ceiling. The deepest legitimate path is roughly
#: graph → nodes → node → config → blocks → block → cards → card → url_button,
#: so 20 leaves generous headroom while stopping a document built to exhaust a
#: parser's stack.
MAX_GRAPH_DEPTH = 20

#: Node and edge ceilings — "generous, e.g. 500 nodes" per the issue.
MAX_NODES = 500
MAX_EDGES = 2000

#: The only keys the envelope carries.
GRAPH_KEYS = ("schema", "nodes", "edges")
_NODE_KEYS = ("id", "type", "position", "config")
_EDGE_KEYS = ("id", "source", "sourceHandle", "target")

# Node and edge ids reach idempotency keys (SPEC §9.4:
# ``exec:{execution_id}:node:{node_id}``) and sticky-randomizer variable names
# (``rand:<node_id>``), so they are an allowlist rather than "any string".
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def limits() -> dict[str, int]:
    """The caps, as one dict.

    The single source for both client-facing copies: the builder reads them from
    ``GET /api/flows/<id>/`` and from ``x-brightbean.limits`` in the exported
    schema, and two hand-maintained copies of the same four numbers is how those
    two start disagreeing about what the server will accept.
    """
    return {
        "max_graph_bytes": MAX_GRAPH_BYTES,
        "max_graph_depth": MAX_GRAPH_DEPTH,
        "max_nodes": MAX_NODES,
        "max_edges": MAX_EDGES,
        "schema_version": SCHEMA_VERSION,
    }


def empty_graph() -> dict[str, Any]:
    """The graph a freshly created flow starts with.

    Valid as a document and saveable; it has no entry node, so publishing it is
    refused until the author puts something on the canvas.
    """
    return {"schema": SCHEMA_VERSION, "nodes": [], "edges": []}


def graph_byte_size(graph: Any) -> int:
    """Serialized size in bytes, or ``-1`` when the value is not JSON at all."""
    try:
        return len(json.dumps(graph, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        return -1


def json_depth(value: Any, *, limit: int) -> int:
    """Maximum container nesting, counted iteratively and abandoned past ``limit``.

    Returns ``limit + 1`` as soon as the document is known to be too deep, so a
    pathological input costs a bounded walk rather than an unbounded one.
    """
    stack: list[tuple[Any, int]] = [(value, 1)]
    deepest = 0
    while stack:
        current, depth = stack.pop()
        if depth > deepest:
            deepest = depth
            if deepest > limit:
                return deepest
        if isinstance(current, dict):
            stack.extend((child, depth + 1) for child in current.values())
        elif isinstance(current, list):
            stack.extend((child, depth + 1) for child in current)
    return deepest


def _issue(code: str, message: str, **address: Any) -> Issue:
    return Issue(code=code, message=message, stage="document", **address)


def check_limits(graph: Any, *, known_size: int | None = None) -> list[Issue]:
    """Size and depth caps (SECURITY-BASELINE §7), before anything walks the graph.

    ``known_size`` is an **upper bound** the caller already has — the API knows
    ``len(request.body)``, and the graph is a subtree of that body, so it cannot
    be larger. Supplying it skips a full re-serialisation of a document that was
    parsed from JSON milliseconds earlier, which on the two-second autosave loop
    (SPEC §16) is the difference between serialising every save once and twice.
    A bound that is itself over the cap falls through to measuring precisely, so
    the error can name the real size.
    """
    if known_size is None or known_size > MAX_GRAPH_BYTES:
        size = graph_byte_size(graph)
        if size < 0:
            return [_issue("malformed_graph", "The graph is not a JSON document.")]
        if size > MAX_GRAPH_BYTES:
            return [
                _issue(
                    "graph_too_large",
                    f"The graph is {size} bytes; the limit is {MAX_GRAPH_BYTES} bytes.",
                )
            ]
    depth = json_depth(graph, limit=MAX_GRAPH_DEPTH)
    if depth > MAX_GRAPH_DEPTH:
        return [_issue("graph_too_deep", f"The graph nests deeper than the limit of {MAX_GRAPH_DEPTH} levels.")]
    return []


def validate_document(graph: Any, *, known_size: int | None = None) -> list[Issue]:
    """Limits, envelope shape and every node config. Findings here refuse the write."""
    issues = check_limits(graph, known_size=known_size)
    if issues:
        return issues

    if not isinstance(graph, dict):
        return [_issue("graph_not_object", "A flow graph must be a JSON object.")]

    for key in graph:
        if key not in GRAPH_KEYS:
            issues.append(
                _issue(
                    "unknown_top_level_key",
                    f"{key!r} is not part of the graph envelope. Allowed: {', '.join(GRAPH_KEYS)}.",
                    path=str(key),
                )
            )

    if graph.get("schema") != SCHEMA_VERSION:
        issues.append(
            _issue(
                "unsupported_schema_version",
                f'Expected "schema": {SCHEMA_VERSION}, got {graph.get("schema")!r}.',
                path="schema",
            )
        )

    nodes = graph.get("nodes")
    edges = graph.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        if not isinstance(nodes, list):
            issues.append(_issue("nodes_not_list", '"nodes" must be a list.', path="nodes"))
        if not isinstance(edges, list):
            issues.append(_issue("edges_not_list", '"edges" must be a list.', path="edges"))
        return issues
    if issues:
        return issues

    if len(nodes) > MAX_NODES:
        issues.append(_issue("too_many_nodes", f"{len(nodes)} nodes; the limit is {MAX_NODES}.", path="nodes"))
    if len(edges) > MAX_EDGES:
        issues.append(_issue("too_many_edges", f"{len(edges)} edges; the limit is {MAX_EDGES}.", path="edges"))
    if issues:
        return issues

    issues.extend(_check_nodes(nodes))
    issues.extend(_check_edges(edges))
    return issues


def _check_nodes(nodes: list[Any]) -> list[Issue]:
    issues: list[Issue] = []
    defs = all_defs()
    seen: set[str] = set()

    for index, node in enumerate(nodes):
        path = f"nodes[{index}]"
        if not isinstance(node, dict):
            issues.append(_issue("node_not_object", "A node must be a JSON object.", path=path))
            continue

        node_id = node.get("id")
        if not isinstance(node_id, str) or not _ID_RE.match(node_id):
            issues.append(
                _issue(
                    "invalid_node_id",
                    "A node id must be 1–64 characters of letters, digits, '_' or '-'.",
                    path=f"{path}.id",
                )
            )
            node_id = None
        elif node_id in seen:
            issues.append(
                _issue("duplicate_node_id", f"Node id {node_id!r} appears more than once.", node_id=node_id, path=path)
            )
        else:
            seen.add(node_id)

        for key in node:
            if key not in _NODE_KEYS:
                issues.append(
                    _issue(
                        "unknown_node_key",
                        f"{key!r} is not part of a node. Allowed: {', '.join(_NODE_KEYS)}.",
                        node_id=node_id,
                        path=f"{path}.{key}",
                    )
                )

        issues.extend(_check_position(node.get("position"), path, node_id))

        spec = node_spec(node.get("type"))
        if spec is None:
            issues.append(
                _issue(
                    "unknown_node_type",
                    f"{node.get('type')!r} is not a known node type.",
                    node_id=node_id,
                    path=f"{path}.type",
                )
            )
            continue

        if "config" not in node:
            issues.append(
                _issue("missing_required_config", '"config" is required.', node_id=node_id, path=f"{path}.config")
            )
            continue

        issues.extend(validate_instance(spec.config, node["config"], path=f"{path}.config", node_id=node_id, defs=defs))

    return issues


def _check_position(position: Any, path: str, node_id: str | None) -> list[Issue]:
    if not isinstance(position, dict):
        return [
            _issue("invalid_position", '"position" must be an {x, y} object.', node_id=node_id, path=f"{path}.position")
        ]
    issues: list[Issue] = []
    for axis in ("x", "y"):
        value = position.get(axis)
        if not isinstance(value, int | float) or isinstance(value, bool):
            issues.append(
                _issue(
                    "invalid_position",
                    f'"position.{axis}" must be a number.',
                    node_id=node_id,
                    path=f"{path}.position.{axis}",
                )
            )
    for key in position:
        if key not in ("x", "y"):
            issues.append(
                _issue(
                    "unknown_node_key",
                    f"{key!r} is not part of a position.",
                    node_id=node_id,
                    path=f"{path}.position.{key}",
                )
            )
    return issues


def _check_edges(edges: list[Any]) -> list[Issue]:
    issues: list[Issue] = []
    seen: set[str] = set()

    for index, edge in enumerate(edges):
        path = f"edges[{index}]"
        if not isinstance(edge, dict):
            issues.append(_issue("edge_not_object", "An edge must be a JSON object.", path=path))
            continue

        edge_id = edge.get("id")
        if not isinstance(edge_id, str) or not _ID_RE.match(edge_id):
            issues.append(
                _issue(
                    "invalid_edge_id",
                    "An edge id must be 1–64 characters of letters, digits, '_' or '-'.",
                    path=f"{path}.id",
                )
            )
            edge_id = None
        elif edge_id in seen:
            issues.append(
                _issue("duplicate_edge_id", f"Edge id {edge_id!r} appears more than once.", edge_id=edge_id, path=path)
            )
        else:
            seen.add(edge_id)

        for key in edge:
            if key not in _EDGE_KEYS:
                issues.append(
                    _issue(
                        "unknown_edge_key",
                        f"{key!r} is not part of an edge. Allowed: {', '.join(_EDGE_KEYS)}.",
                        edge_id=edge_id,
                        path=f"{path}.{key}",
                    )
                )

        for key in ("source", "sourceHandle", "target"):
            if not isinstance(edge.get(key), str) or not edge[key]:
                issues.append(
                    _issue(
                        "invalid_edge_endpoint",
                        f'"{key}" must be a non-empty string.',
                        edge_id=edge_id,
                        path=f"{path}.{key}",
                    )
                )

    return issues
