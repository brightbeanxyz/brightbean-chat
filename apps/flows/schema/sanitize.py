"""Normalising the markup a graph is allowed to store.

Almost every string in ``graph_json`` is text, and text is escaped wherever it
is rendered. A handful are **markup** — today exactly one, ``send_email``'s
``html_body`` — and those are declared by
:attr:`apps.flows.schema.nodes.NodeSpec.html_fields`.

--------------------------------------------------------------------------
Why this runs on the way in
--------------------------------------------------------------------------

``apps/channels/providers/email_html.py`` sanitizes at *send* time, and argued
that the author's own HTML was a lesser threat partly because "our own UI never
renders this string as HTML". The flow builder's body editor made that sentence
false: it writes the stored value into a ``contentEditable`` with ``innerHTML``,
so whatever one workspace member stores is markup that executes in another
member's browser when they open the flow.

Sanitizing at render time in the editor is necessary but not sufficient, because
the builder is not the only writer. ``PUT`` to the flow API takes a whole
``graph_json`` document, and anyone with ``edit_flows`` can call it directly with
a body the editor would never have produced. The authoritative fix therefore has
to be here, on the write path that every client shares.

--------------------------------------------------------------------------
Normalise, do not reject
--------------------------------------------------------------------------

A rejected save is a rejected *autosave*: the builder saves two seconds after a
keystroke, so refusing a document would strand an author with an unsaveable
draft and no obvious cause. Normalising stores what the allowlist permits and
lets the next load show the author exactly what survived — which is also what
they will get in the email, because the same allowlist runs there.

The function is idempotent: re-sanitizing an already-clean document returns it
unchanged, so a save that does not touch the body does not churn it.
"""

from typing import Any

from apps.flows.schema.nodes import node_spec

__all__ = ["sanitize_graph"]


def sanitize_graph(graph: Any) -> Any:
    """Return ``graph`` with every declared HTML field passed through the allowlist.

    Defensive about shape throughout: this runs before schema validation, on a
    document that may be anything at all, and a malformed graph is validation's
    problem to report rather than this function's to crash on.

    Returns the input unchanged when there is nothing to normalise, so the
    common case — a graph with no ``send_email`` node — allocates nothing.
    """
    if not isinstance(graph, dict):
        return graph
    nodes = graph.get("nodes")
    if not isinstance(nodes, list):
        return graph

    changed = False
    cleaned_nodes: list[Any] = []
    for node in nodes:
        cleaned = _sanitize_node(node)
        changed = changed or cleaned is not node
        cleaned_nodes.append(cleaned)
    if not changed:
        return graph
    return {**graph, "nodes": cleaned_nodes}


def _sanitize_node(node: Any) -> Any:
    if not isinstance(node, dict):
        return node
    spec = node_spec(str(node.get("type") or ""))
    if spec is None or not spec.html_fields:
        return node
    config = node.get("config")
    if not isinstance(config, dict):
        return node

    # Imported here rather than at module scope: this module is part of the
    # schema contract, which the flow-schema export command loads on its own,
    # and the sanitizer lives in the channels app. Deferring keeps that export
    # from pulling in the email provider for a function it never calls.
    from apps.channels.providers.email_html import sanitize

    updates = {}
    for name in spec.html_fields:
        value = config.get(name)
        if not isinstance(value, str) or not value:
            continue
        cleaned = sanitize(value)
        if cleaned != value:
            updates[name] = cleaned
    if not updates:
        return node
    return {**node, "config": {**config, **updates}}
