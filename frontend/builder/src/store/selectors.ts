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

import { nodeSpec } from "../schema/artifact";
import { entryNodeIds } from "../schema/entry";
import type { BuilderState } from "./store";

export interface CardData extends Record<string, unknown> {
  nodeId: string;
}

interface CacheEntry {
  key: string;
  node: Node<CardData>;
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

export function selectRfNodes(state: BuilderState): Node<CardData>[] {
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

    const node: Node<CardData> = {
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
let entryCache: { nodeOrder: unknown; edgeOrder: unknown; edge: unknown; ids: Set<string> } | null = null;

export function selectEntryIds(state: BuilderState): Set<string> {
  if (
    entryCache &&
    entryCache.nodeOrder === state.nodeOrder &&
    entryCache.edgeOrder === state.edgeOrder &&
    entryCache.edge === state.edge
  ) {
    return entryCache.ids;
  }
  const ids = entryNodeIds(
    state.nodeOrder.map((id) => ({
      id,
      type: state.nodeType[id] as string,
      position: state.position[id] ?? { x: 0, y: 0 },
      config: state.config[id],
    })),
    state.edgeOrder.flatMap((id) => (state.edge[id] ? [state.edge[id]] : [])) as never,
  );
  entryCache = { nodeOrder: state.nodeOrder, edgeOrder: state.edgeOrder, edge: state.edge, ids };
  return ids;
}
