/**
 * Entry-node detection — a port of
 * :func:`apps.flows.schema.validation._entry_nodes`.
 *
 * SPEC §9.1: the entry node is the one with no incoming edges *from another
 * node*. Two clauses carry all the subtlety, and both are in the Python:
 * a self-edge is not an incoming edge, and an edge from a note does not count
 * either (a note takes part in no routing at all).
 *
 * This only drives the "Start" flag on a card. The server owns the verdict, and
 * says so through `no_entry_node` / `multiple_entry_nodes`, so nothing here
 * restates its message.
 */
import { nodeSpec } from "./artifact";
import type { DomainEdge, DomainNode } from "./types";

export function entryNodeIds(nodes: readonly DomainNode[], edges: readonly DomainEdge[]): Set<string> {
  const annotation = new Set(nodes.filter((node) => nodeSpec(node.type)?.annotation).map((node) => node.id));

  const hasIncoming = new Set<string>();
  for (const edge of edges) {
    if (edge.source === edge.target || annotation.has(edge.source)) {
      continue;
    }
    hasIncoming.add(edge.target);
  }

  return new Set(
    nodes.filter((node) => !annotation.has(node.id) && !hasIncoming.has(node.id)).map((node) => node.id),
  );
}
