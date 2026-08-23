"""Graph validation: what is wrong, and whether it is wrong enough to stop you.

Three tiers, and the difference between the first two is the whole design (see
:mod:`apps.flows.schema.issues`):

* **document errors** — the payload is not storable. Refuse the write.
* **graph errors** — the graph stores fine but will not run. Save it; refuse to
  publish it.
* **warnings** — capability mismatches and unreachable nodes. Never block
  anything (SPEC §9.1: "channel-capability warnings (non-blocking)").

Saving a half-wired graph is not leniency, it is the requirement: SPEC §16 has
the builder autosaving every two seconds, so a draft is *expected* to be caught
mid-edit with an edge hanging off a node the author has not placed yet. Refusing
that save would delete work. Refusing to publish it is a different question, and
the answer there is no.

Cycles are allowed and unremarked — SPEC §9.1 says so explicitly, and the
runtime protects itself with the ``blocks_since_pause`` cap (§9.2) rather than
with a validator that cannot tell a loop from a retry.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from apps.flows.capabilities import capabilities_for
from apps.flows.schema.envelope import validate_document
from apps.flows.schema.handles import parse_handle
from apps.flows.schema.issues import Issue
from apps.flows.schema.nodes import NodeSpec, handles_for_node, node_spec

__all__ = ["ValidationResult", "entry_node_id", "validate_graph"]


@dataclass(frozen=True)
class ValidationResult:
    """Everything validation found, split by what it is allowed to stop."""

    document_errors: list[Issue] = field(default_factory=list)
    graph_errors: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)

    @property
    def errors(self) -> list[Issue]:
        """Everything that blocks publish, in the order it was found."""
        return [*self.document_errors, *self.graph_errors]

    @property
    def blocks_save(self) -> bool:
        """True when the payload must not be persisted at all."""
        return bool(self.document_errors)

    @property
    def is_publishable(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, list[dict[str, Any]]]:
        """The ``{errors, warnings}`` payload the builder renders."""
        return {
            "errors": [issue.as_dict() for issue in self.errors],
            "warnings": [issue.as_dict() for issue in self.warnings],
        }


def validate_graph(graph: Any, *, platforms: Sequence[str] = (), known_size: int | None = None) -> ValidationResult:
    """Validate a graph. ``platforms`` drives capability warnings and nothing else.

    An empty ``platforms`` means "do not guess": no capability warning is
    emitted. That is the state a deployment is in until issue #4 lands
    ``ChannelConnection`` — see
    :func:`apps.flows.capabilities.connected_platforms`.

    ``known_size`` is passed through to the size cap; see
    :func:`apps.flows.schema.envelope.check_limits`.
    """
    document_errors = validate_document(graph, known_size=known_size)
    if document_errors:
        # Nothing below can be trusted to be the shape it looks like, and a
        # cascade of consequential complaints would bury the real one.
        return ValidationResult(document_errors=document_errors)

    nodes: list[dict[str, Any]] = list(graph["nodes"])
    edges: list[dict[str, Any]] = list(graph["edges"])
    specs: dict[str, NodeSpec] = {}
    configs: dict[str, Any] = {}
    for node in nodes:
        spec = node_spec(node["type"])
        if spec is not None:
            specs[node["id"]] = spec
            configs[node["id"]] = node.get("config")

    # Computed once and handed to both consumers: "which nodes are entries" is
    # one definition, and two copies of it drift the moment one is corrected.
    routable = _routable(specs)
    entries = _entry_nodes(specs, edges, routable)

    errors: list[Issue] = []
    errors.extend(_check_edges(edges, specs, configs))
    errors.extend(_check_entry_nodes(routable, entries))
    errors.extend(_check_reply_ids(nodes))

    warnings: list[Issue] = []
    warnings.extend(_capability_warnings(nodes, platforms))
    warnings.extend(_unreachable_warnings(specs, edges, routable, entries))

    return ValidationResult(graph_errors=errors, warnings=warnings)


# ---------------------------------------------------------------------------
# Graph errors
# ---------------------------------------------------------------------------


def _check_edges(edges: list[dict[str, Any]], specs: dict[str, NodeSpec], configs: dict[str, Any]) -> list[Issue]:
    issues: list[Issue] = []
    taken: dict[tuple[str, str], str] = {}

    for edge in edges:
        edge_id = edge["id"]
        source, target, raw_handle = edge["source"], edge["target"], edge["sourceHandle"]

        missing = [end for end, value in (("source", source), ("target", target)) if value not in specs]
        if missing:
            issues.append(
                Issue(
                    code="dangling_edge",
                    message=f"Edge {edge_id!r} names no such node ({', '.join(missing)}).",
                    edge_id=edge_id,
                )
            )
            continue

        for end, node_id in (("source", source), ("target", target)):
            if specs[node_id].annotation:
                issues.append(
                    Issue(
                        code="note_node_connected",
                        message=(
                            f"Edge {edge_id!r} connects to note {node_id!r}. A note is a builder-only "
                            f"annotation and takes no part in routing (SPEC §11.11)."
                        ),
                        edge_id=edge_id,
                        node_id=node_id,
                        path=end,
                    )
                )

        if specs[source].terminal:
            issues.append(
                Issue(
                    code="terminal_node_has_outgoing_edge",
                    message=(
                        f"Node {source!r} is a {specs[source].type} node, which ends the execution, "
                        f"so edge {edge_id!r} can never be followed."
                    ),
                    edge_id=edge_id,
                    node_id=source,
                )
            )
            continue

        if parse_handle(raw_handle) is None:
            issues.append(
                Issue(
                    code="malformed_handle",
                    message=(
                        f"{raw_handle!r} is not a handle. Expected default, timeout, error, cond:true, "
                        f"cond:false, or btn:/qr:/rand: followed by an id."
                    ),
                    edge_id=edge_id,
                    node_id=source,
                )
            )
            continue

        available = handles_for_node(specs[source], configs.get(source))
        if raw_handle not in available:
            issues.append(
                Issue(
                    code="handle_not_available",
                    message=(
                        f"Node {source!r} does not expose {raw_handle!r}. "
                        f"It offers: {', '.join(sorted(available)) or 'nothing'}."
                    ),
                    edge_id=edge_id,
                    node_id=source,
                )
            )
            continue

        previous = taken.get((source, raw_handle))
        if previous is not None:
            issues.append(
                Issue(
                    code="duplicate_handle_edge",
                    message=(
                        f"Edges {previous!r} and {edge_id!r} both leave node {source!r} by {raw_handle!r}. "
                        f"The runner follows one edge per handle, so which one is undefined."
                    ),
                    edge_id=edge_id,
                    node_id=source,
                )
            )
        else:
            taken[(source, raw_handle)] = edge_id

    return issues


def entry_node_id(graph: Any) -> str | None:
    """Where a run of ``graph`` starts, or ``None`` when that is not a question.

    The engine (L3-B) needs the same answer the validator computes, and two
    implementations of "the node with no incoming edge from another node" would
    drift the moment one of the exclusions below was corrected in only one of
    them — a graph that publishes cleanly and then starts at the wrong node is
    about the worst shape that disagreement could take.

    So this is a thin wrapper over :func:`_entry_nodes`, not a second reading of
    SPEC §9.1. ``None`` means the graph has zero entries or several, which is a
    graph error :func:`validate_graph` already reports; the caller is expected
    to have published through that gate and to treat ``None`` as a broken graph
    rather than as a normal state.
    """
    if not isinstance(graph, dict):
        return None
    nodes = graph.get("nodes")
    edges = graph.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        return None

    specs: dict[str, NodeSpec] = {}
    for node in nodes:
        if not isinstance(node, dict) or not isinstance(node.get("id"), str):
            continue
        spec = node_spec(node.get("type"))
        if spec is not None:
            specs[node["id"]] = spec

    usable = [edge for edge in edges if isinstance(edge, dict) and "source" in edge and "target" in edge]
    entries = _entry_nodes(specs, usable, _routable(specs))
    return entries[0] if len(entries) == 1 else None


def _check_reply_ids(nodes: list[dict[str, Any]]) -> list[Issue]:
    """A button and a quick reply on one node may not share an id.

    Both are legal handles — ``btn:<id>`` and ``qr:<id>`` are separate edges —
    but a platform sends back only the id when either is used
    (``EventPayload.button_id``), with nothing to say which control produced it.
    So a node carrying both cannot be routed: the engine has to guess, and half
    the time it guesses wrong and follows the other branch.

    A graph error rather than a warning, because the consequence is a message
    going to the wrong place rather than a cosmetic mismatch — and rather than a
    document error, because a half-wired draft must still save (SPEC §16's
    two-second autosave).
    """
    issues: list[Issue] = []
    for node in nodes:
        config = node.get("config")
        if not isinstance(config, dict):
            continue
        button_ids = {
            item["id"]
            for item in config.get("buttons") or []
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        clashing = sorted(
            item["id"]
            for item in config.get("quick_replies") or []
            if isinstance(item, dict) and item.get("id") in button_ids
        )
        issues.extend(
            Issue(
                code="duplicate_reply_id",
                message=(
                    f"{reply_id!r} is both a button id and a quick reply id on this node. An inbound "
                    f"reply carries only the id, so the two cannot be told apart — give one of them a "
                    f"different id."
                ),
                node_id=node.get("id"),
                path="quick_replies",
            )
            for reply_id in clashing
        )
    return issues


def _routable(specs: dict[str, NodeSpec]) -> list[str]:
    """Node ids that take part in routing — everything but the annotations."""
    return [node_id for node_id, spec in specs.items() if not spec.annotation]


def _entry_nodes(specs: dict[str, NodeSpec], edges: list[dict[str, Any]], routable: list[str]) -> list[str]:
    """Routable nodes with no incoming edge **from another node** (SPEC §9.1).

    Two exclusions, both from that phrase rather than from convenience:

    * A **self-edge** is not an edge from another node. Counting one would make
      the commonest retry shape there is — a question node whose ``timeout``
      handle re-asks itself — report "there is nowhere to start" and refuse to
      publish.
    * An edge from a **note** is not routing at all (SPEC §11.11), and every
      other part of this module already excludes annotations. Counting one made
      connecting a note to the first node report a missing entry node on top of
      the note error that actually describes the mistake.
    """
    targeted = {
        edge["target"]
        for edge in edges
        if edge["target"] in specs
        and edge["source"] in specs
        and edge["source"] != edge["target"]
        and not specs[edge["source"]].annotation
    }
    return [node_id for node_id in routable if node_id not in targeted]


def _check_entry_nodes(routable: list[str], entries: list[str]) -> list[Issue]:
    """SPEC §9.1: exactly one node with no incoming edge from another node."""
    if not routable:
        return [
            Issue(
                code="no_entry_node",
                message="The flow has no nodes to run. Add one before publishing.",
            )
        ]

    if not entries:
        return [
            Issue(
                code="no_entry_node",
                message=(
                    "Every node is only reachable from another node, so there is nowhere to start. "
                    "Exactly one node must have no incoming edge — break the loop that returns to "
                    "the node the flow should begin at (SPEC §9.1)."
                ),
            )
        ]
    if len(entries) > 1:
        listed = ", ".join(sorted(entries))
        return [
            Issue(
                code="multiple_entry_nodes",
                message=f"{len(entries)} nodes have no incoming edge ({listed}); exactly one may (SPEC §9.1).",
                node_id=node_id,
            )
            for node_id in sorted(entries)
        ]
    return []


# ---------------------------------------------------------------------------
# Warnings
# ---------------------------------------------------------------------------


def _unreachable_warnings(
    specs: dict[str, NodeSpec], edges: list[dict[str, Any]], routable_ids: list[str], entries: list[str]
) -> list[Issue]:
    """Nodes no path from the entry reaches. Harmless, but almost always a mistake."""
    routable = set(routable_ids)
    if len(entries) != 1:
        # Zero or several entries is already a graph error; a second complaint
        # about the same shape adds noise, not information.
        return []

    outgoing: dict[str, list[str]] = {}
    for edge in edges:
        if edge["source"] in routable and edge["target"] in routable:
            outgoing.setdefault(edge["source"], []).append(edge["target"])

    seen = {entries[0]}
    queue = [entries[0]]
    while queue:
        current = queue.pop()
        for target in outgoing.get(current, ()):
            if target not in seen:
                seen.add(target)
                queue.append(target)

    return [
        Issue(
            code="unreachable_node",
            message=f"Nothing routes to node {node_id!r}, so it will never run.",
            node_id=node_id,
        )
        for node_id in sorted(routable - seen)
    ]


def _capability_warnings(nodes: list[dict[str, Any]], platforms: Sequence[str]) -> list[Issue]:
    """Channel-capability mismatches, read from the static table (ROADMAP contract 4).

    Two different questions, deliberately kept apart. *Can this platform render
    what the node asks for* is asked once per connected platform, because a
    workspace on Telegram and SMS wants to hear about the SMS limitation without
    the Telegram column going quiet. *Is there a connection of the right kind at
    all* is asked once for the graph, because "you have no SMS channel" is one
    fact, not one per platform.
    """
    issues: list[Issue] = []
    for platform in platforms:
        capabilities = capabilities_for(platform)
        if capabilities is None:
            continue
        for node in nodes:
            if node.get("type") == "send_message" and isinstance(node.get("config"), dict):
                issues.extend(_send_message_warnings(node, node["config"], platform, capabilities))

    if platforms:
        for node in nodes:
            node_type = node.get("type")
            required = _REQUIRED_PLATFORM.get(node_type) if isinstance(node_type, str) else None
            if required is not None and required not in platforms:
                issues.append(
                    _warn(
                        "no_connection_for_node",
                        f"This node needs a {required} connection and the workspace has none (SPEC §11.9, §11.10).",
                        node,
                    )
                )
    return issues


#: Node types that only run on one platform, and which one.
_REQUIRED_PLATFORM = {"send_sms": "sms", "send_email": "email"}


def _warn(code: str, message: str, node: dict[str, Any], path: str | None = None) -> Issue:
    return Issue(code=code, message=message, node_id=node.get("id"), path=path)


def _send_message_warnings(
    node: dict[str, Any], config: dict[str, Any], platform: str, capabilities: Any
) -> Iterable[Issue]:
    for index, block in enumerate(config.get("blocks") or []):
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if isinstance(block_type, str) and block_type != "text" and not capabilities.supports_block(block_type):
            yield _warn(
                "capability_unsupported",
                f"{platform} has no native {block_type} block; the adapter downgrades it (SPEC §6.1).",
                node,
                f"config.blocks[{index}]",
            )
        text = block.get("text")
        if isinstance(text, str) and len(text) > capabilities.max_text_len:
            yield _warn(
                "capability_limit_exceeded",
                f"{platform} truncates at {capabilities.max_text_len} characters; this block has {len(text)}.",
                node,
                f"config.blocks[{index}].text",
            )

    if capabilities.interaction_is_exclusive and (config.get("buttons") or []) and (config.get("quick_replies") or []):
        # Otherwise the panel says "WhatsApp allows 10 quick replies" and stays
        # quiet, while every one of them actually arrives as numbered text
        # because the buttons took the only control set the message has.
        yield _warn(
            "capability_unsupported",
            f"{platform} shows buttons or quick replies, not both; the adapter appends the quick "
            f"replies to the text as numbered options instead (SPEC §6.1).",
            node,
            "config.quick_replies",
        )

    for key, supported, ceiling, label in (
        ("buttons", capabilities.buttons, capabilities.max_buttons, "buttons"),
        ("quick_replies", capabilities.quick_replies, capabilities.max_quick_replies, "quick replies"),
    ):
        items = config.get(key) or []
        if not items:
            continue
        if not supported:
            yield _warn(
                "capability_unsupported",
                f"{platform} does not support {label}; the adapter appends them to the text instead (SPEC §6.1).",
                node,
                f"config.{key}",
            )
        elif len(items) > ceiling:
            yield _warn(
                "capability_limit_exceeded",
                f"{platform} allows {ceiling} {label}; this node has {len(items)}.",
                node,
                f"config.{key}",
            )

    if not capabilities.url_buttons:
        for index, button in enumerate(config.get("buttons") or []):
            if isinstance(button, dict) and button.get("action") == "url":
                yield _warn(
                    "capability_unsupported",
                    f"{platform} has no link buttons; the URL is appended to the text instead.",
                    node,
                    f"config.buttons[{index}]",
                )
