/**
 * The only place wire shapes are built, and the reason round-trip fidelity holds.
 *
 * `toGraph` **constructs** each node and edge from four named fields. It never
 * takes a React Flow object and strips it, which is the usual shape of this code
 * and the usual source of the bug: apps/flows/schema/envelope.py closes every
 * object, so a stray `selected`, `dragging`, `measured`, `width` or `data`
 * reaching a PUT is `unknown_node_key` — a 422 that discards the entire save,
 * not a field the server quietly ignores.
 *
 * `fromGraph` is deliberately lossless in the other direction: configs are
 * stored by reference and never normalised. There is no `applyDefaults()` here
 * on purpose — filling in `sticky: false` on load would make load → save a
 * rewrite, and reload → compare would no longer reproduce what the user saved.
 */
import { SCHEMA_VERSION } from "../schema/artifact";
import type { DomainEdge, DomainNode, FlowGraph, Position } from "../schema/types";

export interface GraphState {
  /** Serialisation order, kept stable so a save is not a reshuffle. */
  nodeOrder: string[];
  nodeType: Record<string, string>;
  config: Record<string, unknown>;
  /**
   * Split from the other node maps because it changes on every drag frame. A
   * panel subscribed to `config` therefore cannot re-render during a drag,
   * which is what keeps a 100-node graph responsive.
   */
  position: Record<string, Position>;
  edgeOrder: string[];
  edge: Record<string, DomainEdge>;
  /** Echoed from the loaded graph, never invented. */
  schemaVersion: number;
}

export function emptyGraphState(): GraphState {
  return {
    nodeOrder: [],
    nodeType: {},
    config: {},
    position: {},
    edgeOrder: [],
    edge: {},
    schemaVersion: SCHEMA_VERSION,
  };
}

/**
 * Round a coordinate and refuse a non-finite one.
 *
 * React Flow can hand back `NaN` when a pointer event is processed before the
 * pane has been measured, and `non_finite_number` is a document-stage error —
 * it would discard the save. Two decimals also keeps a hundred nodes from
 * spending kilobytes of the 512 KiB budget on float noise.
 */
export function sanitizePosition(position: Partial<Position> | undefined): Position {
  const round = (value: unknown) => (typeof value === "number" && Number.isFinite(value) ? Math.round(value * 100) / 100 : 0);
  return { x: round(position?.x), y: round(position?.y) };
}

export function fromGraph(graph: FlowGraph): GraphState {
  const state = emptyGraphState();
  state.schemaVersion = graph.schema;

  for (const node of graph.nodes ?? []) {
    state.nodeOrder.push(node.id);
    state.nodeType[node.id] = node.type;
    state.config[node.id] = node.config;
    state.position[node.id] = sanitizePosition(node.position);
  }
  for (const edge of graph.edges ?? []) {
    state.edgeOrder.push(edge.id);
    state.edge[edge.id] = {
      id: edge.id,
      source: edge.source,
      sourceHandle: edge.sourceHandle,
      target: edge.target,
    };
  }
  return state;
}

export function toGraph(state: GraphState): FlowGraph {
  const nodes: DomainNode[] = [];
  for (const id of state.nodeOrder) {
    const type = state.nodeType[id];
    if (type === undefined) {
      continue;
    }
    nodes.push({
      id,
      type,
      position: state.position[id] ?? { x: 0, y: 0 },
      config: state.config[id],
    });
  }

  const edges: DomainEdge[] = [];
  for (const id of state.edgeOrder) {
    const edge = state.edge[id];
    if (edge === undefined) {
      continue;
    }
    // Four named fields, listed here and nowhere else.
    edges.push({ id: edge.id, source: edge.source, sourceHandle: edge.sourceHandle, target: edge.target });
  }

  return { schema: state.schemaVersion, nodes, edges };
}

/** The graph's size on the wire, for the pre-flight against `max_graph_bytes`. */
export function graphByteLength(graph: FlowGraph): number {
  return new TextEncoder().encode(JSON.stringify(graph)).length;
}
