/**
 * A graph that exercises every node type the artefact declares.
 *
 * Generated from `x-brightbean.node_types` rather than written out, so a node
 * type added by a later layer is covered by the round-trip suite the moment
 * `make schema` runs — which is the only way "every node type serialises valid
 * config" can stay true without someone remembering to extend a fixture.
 */
import { NODE_TYPES, SCHEMA_VERSION, nodeSpec } from "../schema/artifact";
import { sourceHandles } from "../schema/handles";
import { sampleConfig } from "../schema/sample";
import type { DomainEdge, DomainNode, FlowDetail, FlowGraph, Picklists } from "../schema/types";

/** Deterministic ids, so a snapshot is stable across runs. */
export function sequentialIds(prefix: string): () => string {
  let counter = 0;
  return () => `${prefix}${(counter += 1)}`;
}

export interface SampleGraphOptions {
  optional?: boolean;
  variant?: number;
}

/**
 * One node per type, plus an edge for every handle each node exposes.
 *
 * Edges deliberately point at the next node in the list rather than forming a
 * sensible flow: the point is coverage of the handle grammar, not a runnable
 * graph. Notes are skipped as endpoints, since SPEC §11.11 forbids connecting
 * them.
 */
export function makeSampleGraph(options: SampleGraphOptions = {}): FlowGraph {
  const makeItemId = sequentialIds("i");
  const nodes: DomainNode[] = NODE_TYPES.map((spec, index) => ({
    id: `n${index + 1}`,
    type: spec.type,
    position: { x: index * 240, y: (index % 3) * 160 },
    config: sampleConfig(spec.type, { ...options, makeId: makeItemId }),
  }));

  const connectable = nodes.filter((node) => !nodeSpec(node.type)?.annotation);
  const edges: DomainEdge[] = [];
  let counter = 0;

  nodes.forEach((node) => {
    const spec = nodeSpec(node.type);
    if (!spec || spec.annotation) {
      return;
    }
    for (const handle of sourceHandles(spec, node.config)) {
      const target = connectable.find((candidate) => candidate.id !== node.id);
      if (!target) {
        continue;
      }
      edges.push({ id: `e${(counter += 1)}`, source: node.id, sourceHandle: handle, target: target.id });
    }
  });

  return { schema: SCHEMA_VERSION, nodes, edges };
}

export const EMPTY_PICKLISTS: Picklists = {
  tags: [],
  custom_fields: [],
  sequences: [],
  flows: [],
  connections: [],
  members: [],
};

export function makeDetail(graph: FlowGraph = makeSampleGraph(), overrides: Partial<FlowDetail> = {}): FlowDetail {
  return {
    flow: { id: "flow-1", name: "Welcome", status: "draft", folder: "", updated_at: "2026-08-22T10:00:00+00:00" },
    version: { id: "v1", version: 1, published: false, updated_at: "2026-08-22T10:00:00+00:00" },
    graph,
    published_version: null,
    picklists: EMPTY_PICKLISTS,
    triggers: [],
    validation: { errors: [], warnings: [] },
    limits: {
      schema_version: SCHEMA_VERSION,
      max_graph_bytes: 524288,
      max_graph_depth: 20,
      max_nodes: 500,
      max_edges: 2000,
    },
    schema_url: "/w/ws/api/flows/schema/",
    ...overrides,
  };
}

/** Key order is not preserved by Postgres jsonb, so compare canonical forms. */
export function canonical(value: unknown): unknown {
  if (Array.isArray(value)) {
    return value.map(canonical);
  }
  if (typeof value === "object" && value !== null) {
    return Object.fromEntries(
      Object.keys(value as Record<string, unknown>)
        .sort()
        .map((key) => [key, canonical((value as Record<string, unknown>)[key])]),
    );
  }
  return value;
}
