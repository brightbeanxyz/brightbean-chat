"""The shared graph schema — ROADMAP contract 2.

``flows/schema/`` is the single source of truth for the flow graph format: the
server validates against it, the React builder (L3-C) generates its config
panels from the document :mod:`~apps.flows.schema.export` emits, and the engine
(L3-B) reads the same node registry. Import from this package, not from the
modules underneath it, so the surface later layers depend on stays small:

    from apps.flows.schema import NODE_TYPES, empty_graph, validate_graph

Adding a node type is :func:`~apps.flows.schema.nodes.register_node_type`;
adding an action verb is :func:`~apps.flows.schema.nodes.register_action_verb`.
Both are additive, and both feed validation and the exported document at once.
"""

from apps.flows.schema.condition import CONDITION_SCHEMA
from apps.flows.schema.envelope import (
    MAX_EDGES,
    MAX_GRAPH_BYTES,
    MAX_GRAPH_DEPTH,
    MAX_NODES,
    SCHEMA_VERSION,
    empty_graph,
    limits,
    validate_document,
)
from apps.flows.schema.export import artifact_path, json_schema, serialize
from apps.flows.schema.handles import Handle, parse_handle
from apps.flows.schema.issues import Issue
from apps.flows.schema.nodes import (
    ACTION_VERBS,
    NODE_TYPES,
    NodeSpec,
    handles_for_node,
    node_spec,
    register_action_verb,
    register_node_type,
)
from apps.flows.schema.validation import ValidationResult, entry_node_id, validate_graph

__all__ = [
    "ACTION_VERBS",
    "CONDITION_SCHEMA",
    "MAX_EDGES",
    "MAX_GRAPH_BYTES",
    "MAX_GRAPH_DEPTH",
    "MAX_NODES",
    "SCHEMA_VERSION",
    "Handle",
    "Issue",
    "NODE_TYPES",
    "NodeSpec",
    "ValidationResult",
    "artifact_path",
    "empty_graph",
    "entry_node_id",
    "handles_for_node",
    "json_schema",
    "limits",
    "node_spec",
    "parse_handle",
    "register_action_verb",
    "register_node_type",
    "serialize",
    "validate_document",
    "validate_graph",
]
