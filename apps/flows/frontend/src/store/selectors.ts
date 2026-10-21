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

import { TRIGGER_NODE_ID } from "../canvas/TriggerCard";
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

/**
 * Where the trigger card sits: one column left of the step the flow starts at.
 *
 * Derived from the entry node rather than stored, because it has nowhere to be
 * stored — the trigger is not in the graph (see canvas/TriggerCard.tsx), so
 * there is no `position[id]` for it and no save that could carry one. Moving
 * the entry node moves this with it, which is the behaviour somebody expects
 * from a card that is pinned to it.
 */
const TRIGGER_OFFSET = 340;

function triggerPosition(state: BuilderState): { x: number; y: number } {
  const entries = selectEntryIds(state);
  const first = state.nodeOrder.find((id) => entries.has(id));
  const anchor = first ? state.position[first] : undefined;
  return anchor ? { x: anchor.x - TRIGGER_OFFSET, y: anchor.y } : { x: -TRIGGER_OFFSET, y: 0 };
}

export function selectRfNodes(state: BuilderState): CardNode[] {
  const cache = cacheFor(state);
  const selected = new Set(state.selection.nodes);
  const draggable = state.env.canEdit;

  const nodes = state.nodeOrder.map((id) => {
    const type = state.nodeType[id] as string;
    const position = state.position[id] ?? { x: 0, y: 0 };
    const isSelected = selected.has(id);
    const size = state.measured[id];
    const key = `${type}|${position.x},${position.y}|${isSelected ? 1 : 0}|${draggable ? 1 : 0}|${size ? `${size.width}x${size.height}` : ""}`;

    const hit = cache.get(id);
    if (hit && hit.key === key) {
      return hit.node;
    }

    const node: CardNode = {
      id,
      type,
      position,
      // Handed straight back to React Flow, which is the only consumer: Fit,
      // the minimap and every bounds calculation read it. See the `measured`
      // slice in store.ts for why it stopped being dropped.
      ...(size ? { measured: size } : {}),
      data: { nodeId: id },
      selected: isSelected,
      draggable,
      connectable: draggable && !nodeSpec(type)?.annotation,
      deletable: draggable,
    };
    cache.set(id, { key, node });
    return node;
  });

  return [triggerNode(state, cache), ...nodes];
}

/**
 * The trigger card, cached like every other node.
 *
 * Rebuilding it per call would hand React Flow a new object identity on every
 * store read, which remounts the card — the exact mistake canvas/types.ts warns
 * about for the type maps, and the one performance.test.tsx pins for nodes.
 * Keyed on its position, because that is the only input that can change it; the
 * trigger *list* is read by the component through its own subscription, so a
 * trigger edit re-renders the card without needing a new node object.
 *
 * Injected here and nowhere else. `toGraph()` serializes `nodeOrder`,
 * `nodeType`, `config` and `position`, and this node is in none of them — so no
 * save can carry it, by construction rather than by a filter somebody has to
 * remember.
 */
function triggerNode(state: BuilderState, cache: Map<string, CacheEntry>): CardNode {
  const position = triggerPosition(state);
  const size = state.measured[TRIGGER_NODE_ID];
  const key = `${position.x},${position.y}|${size ? `${size.width}x${size.height}` : ""}`;
  const hit = cache.get(TRIGGER_NODE_ID);
  if (hit && hit.key === key) {
    return hit.node;
  }
  const node: CardNode = {
    id: TRIGGER_NODE_ID,
    type: TRIGGER_NODE_ID,
    position,
    // Measured like any other card, which is what puts it inside Fit's bounds.
    ...(size ? { measured: size } : {}),
    data: { nodeId: TRIGGER_NODE_ID },
    draggable: false,
    connectable: false,
    deletable: false,
    // Selectable, or React Flow gives the wrapper `pointer-events: none` and
    // the card cannot be clicked at all — it computes that from
    // `isSelectable || isDraggable || <a handler it was passed>`, and this node
    // is neither. Safe, because Canvas.tsx drops every change carrying this id
    // before the switch, so React Flow's own selection never reaches the store;
    // `selectTrigger()` on the card owns it instead.
    selectable: true,
  };
  cache.set(TRIGGER_NODE_ID, { key, node });
  return node;
}

export function selectRfEdges(state: BuilderState): Edge[] {
  const selected = new Set(state.selection.edges);

  // The trigger's edge into the step the flow starts at. Only drawn when there
  // is exactly one such step: with none, or with several, there is no single
  // honest answer and validation is already saying so.
  const entries = selectEntryIds(state);
  const entry = entries.size === 1 ? state.nodeOrder.find((id) => entries.has(id)) : undefined;
  const fromTrigger: Edge[] = entry
    ? [
        {
          id: `${TRIGGER_NODE_ID}-starts-${entry}`,
          source: TRIGGER_NODE_ID,
          sourceHandle: "starts",
          target: entry,
          type: "handleLabel",
          selectable: false,
          deletable: false,
          focusable: false,
        } satisfies Edge,
      ]
    : [];

  return fromTrigger.concat(state.edgeOrder.flatMap((id) => {
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
  }));
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

