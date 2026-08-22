/**
 * The React Flow projection.
 *
 * Rebuilt whenever the maps it reads change, but each node object is reused
 * when its own inputs are referentially unchanged — React Flow memoises its
 * node wrapper on that identity, so an edit to one node re-renders one card.
 *
 * `data` is deliberately just `{ nodeId }`. Putting the config, the validation
 * badge or the stats in there would make every card re-render whenever any of
 * them changed anywhere; instead each card subscribes to its own slices.
 */
import type { Edge, Node } from "@xyflow/react";

import type { DomainEdge } from "../schema/types";

import { nodeSpec } from "../schema/artifact";
import { entryNodeIds } from "../schema/entry";
import type { BuilderState } from "./store";

export interface CardData extends Record<string, unknown> {
  nodeId: string;
}

/** The one node shape the canvas deals in. */
export type CardNode = Node<CardData>;

interface CacheEntry {
  key: string;
  node: CardNode;
}

const nodeCache = new WeakMap<object, Map<string, CacheEntry>>();

function cacheFor(state: BuilderState): Map<string, CacheEntry> {
  let cache = nodeCache.get(state.nodeType);
  if (!cache) {
    cache = new Map();
    nodeCache.set(state.nodeType, cache);
  }
  return cache;
}

export function selectRfNodes(state: BuilderState): CardNode[] {
  const cache = cacheFor(state);
  const selected = new Set(state.selection.nodes);
  const draggable = state.env.canEdit;

  return state.nodeOrder.map((id) => {
    const type = state.nodeType[id] as string;
    const position = state.position[id] ?? { x: 0, y: 0 };
    const isSelected = selected.has(id);
    const key = `${type}|${position.x},${position.y}|${isSelected ? 1 : 0}|${draggable ? 1 : 0}`;

    const hit = cache.get(id);
    if (hit && hit.key === key) {
      return hit.node;
    }

    const node: CardNode = {
      id,
      type,
      position,
      data: { nodeId: id },
      selected: isSelected,
      draggable,
      connectable: draggable && !nodeSpec(type)?.annotation,
      deletable: draggable,
    };
    cache.set(id, { key, node });
    return node;
  });
}

export function selectRfEdges(state: BuilderState): Edge[] {
  const selected = new Set(state.selection.edges);
  return state.edgeOrder.flatMap((id) => {
    const edge = state.edge[id];
    if (!edge) {
      return [];
    }
    return [
      {
        id: edge.id,
        source: edge.source,
        sourceHandle: edge.sourceHandle,
        target: edge.target,
        type: "handleLabel",
        selected: selected.has(id),
        deletable: state.env.canEdit,
      } satisfies Edge,
    ];
  });
}

/**
 * The entry nodes, memoised on the two maps that can change the answer.
 *
 * Derived rather than stored so no mutation has to remember to maintain it, and
 * memoised because every card asks — an un-memoised answer would be O(nodes x
 * edges) per render, and would also hand `useSyncExternalStore` a fresh Set
 * every time and loop forever.
 */
/**
 * The entry nodes, memoised per store on the maps that can change the answer.
 *
 * Derived rather than stored so no mutation has to remember to maintain it, and
 * memoised because every card asks — an un-memoised answer would be O(nodes x
 * edges) per render, and would also hand `useSyncExternalStore` a fresh Set
 * every time and loop forever.
 *
 * Keyed by the store's own `nodeType` map rather than a module-level variable:
 * a single shared slot means two stores alive at once — every test that builds
 * more than one — evict each other on alternating reads.
 */
interface EntryCacheEntry {
  nodeOrder: unknown;
  edgeOrder: unknown;
  edge: unknown;
  ids: Set<string>;
}

const entryCache = new WeakMap<object, EntryCacheEntry>();

export function selectEntryIds(state: BuilderState): Set<string> {
  const cached = entryCache.get(state.nodeType);
  if (
    cached &&
    cached.nodeOrder === state.nodeOrder &&
    cached.edgeOrder === state.edgeOrder &&
    cached.edge === state.edge
  ) {
    return cached.ids;
  }

  // entryNodeIds reads only `id` and `type`; position and config would be
  // hundreds of object copies for nothing.
  const nodes = state.nodeOrder.map((id) => ({
    id,
    type: state.nodeType[id] ?? "",
    position: ORIGIN,
    config: undefined,
  }));
  const edges: DomainEdge[] = state.edgeOrder.flatMap((id) => {
    const edge = state.edge[id];
    return edge ? [edge] : [];
  });
  const ids = entryNodeIds(nodes, edges);

  entryCache.set(state.nodeType, { nodeOrder: state.nodeOrder, edgeOrder: state.edgeOrder, edge: state.edge, ids });
  return ids;
}

/** Shared, because entryNodeIds never reads a position. */
const ORIGIN = Object.freeze({ x: 0, y: 0 });

