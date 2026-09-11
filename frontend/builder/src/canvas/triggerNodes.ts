/**
 * The trigger cards, and why they are not real nodes.
 *
 * What *starts* a flow lived behind a button in the top-right corner, so it was
 * not part of the flow anyone was looking at. The sharpest cost: on a
 * comment-to-DM build the public reply is configured on the trigger and appears
 * nowhere in the graph, so the natural conclusion is that the feature does not
 * exist.
 *
 * These cards are synthetic and never enter the persisted graph. The
 * alternative — registering a real `NodeSpec` — costs a new arm on the
 * envelope's tagged union, a regenerated schema artefact, and an entry-node
 * problem with no right answer: with `annotation=false` two triggers read as
 * `multiple_entry_nodes` on a perfectly ordinary flow, and with
 * `annotation=true` the note rules forbid it being an edge endpoint at all,
 * which is the entire point of the card. It would also make the graph a second
 * home for something the `Trigger` table already owns, with nothing reconciling
 * the two — and it would not even remove this code, because every flow that
 * exists today has no trigger node in it.
 *
 * `toGraph()` iterates `nodeOrder`. A card that never enters `nodeOrder` cannot
 * reach the wire, so round-trip fidelity holds by construction rather than by
 * remembering to strip something.
 */
import type { Edge } from "@xyflow/react";

import type { TriggerSummary } from "../schema/types";
import { selectEntryIds, type CardNode } from "../store/selectors";
import type { BuilderState } from "../store/store";

export const TRIGGER_NODE_TYPE = "__trigger";
export const ADD_TRIGGER_NODE_TYPE = "__add_trigger";
export const TRIGGER_EDGE_TYPE = "triggerEdge";

/**
 * `:` is outside the envelope's ID_PATTERN, so a synthetic id can never collide
 * with a persisted one — or be mistaken for one by anything that later decides
 * to look.
 */
export const triggerNodeId = (triggerId: string): string => `trigger:${triggerId}`;
export const ADD_TRIGGER_NODE_ID = "trigger:add";

export interface TriggerCardData extends Record<string, unknown> {
  /** Null on the empty-state card. */
  trigger: TriggerSummary | null;
  canEdit: boolean;
}

/** .fb-node is 15rem; this leaves ~80px of gutter between card and node. */
const TRIGGER_DX = 320;
const TRIGGER_DY = 112;

/**
 * Declared rather than measured, and it matters.
 *
 * React Flow computes `fitView`'s bounds from the dimensions it has, and it
 * only has measured ones after a ResizeObserver callback. A card whose size it
 * does not yet know is simply not in the bounds — so the initial fit framed the
 * graph and left the trigger stack outside the pane, which is the one thing a
 * flow most needs to show. Stating the size up front puts it in the first fit.
 *
 * Must track `.fb-trigger` in styles.css: 13rem wide, and a height that is
 * about right for a two-line summary. Being a little off costs some padding in
 * the initial fit and nothing else — React Flow replaces both with the real
 * measurements as soon as it has them.
 */
const TRIGGER_W = 208;
const TRIGGER_H = 104;

const ORIGIN = { x: 0, y: 0 };

export interface TriggerAnchor {
  at: { x: number; y: number };
  /** The node the edge may point at, or null when that would be a lie. */
  wired: string | null;
}

/**
 * Where the stack sits, and whether an arrow out of it means anything.
 *
 * Derived on every read rather than stored, because it hangs off a node
 * position the author drags around. The fallback is the honest one: with zero
 * or several entry nodes the graph is already in error and the problems rail
 * says so, and drawing an arrow into an arbitrarily chosen node would be
 * asserting something false about which node runs first.
 */
export function triggerAnchor(state: BuilderState): TriggerAnchor {
  if (state.nodeOrder.length === 0) {
    return { at: ORIGIN, wired: null };
  }

  const entries = selectEntryIds(state);
  if (entries.size === 1) {
    const id = [...entries][0] as string;
    return { at: state.position[id] ?? ORIGIN, wired: id };
  }

  let leftmost: string | null = null;
  let best = Number.POSITIVE_INFINITY;
  for (const id of state.nodeOrder) {
    const position = state.position[id] ?? ORIGIN;
    if (position.x < best) {
      best = position.x;
      leftmost = id;
    }
  }
  return { at: (leftmost && state.position[leftmost]) || ORIGIN, wired: null };
}

const cache = new WeakMap<object, { key: string; nodes: CardNode[] }>();

/**
 * One card per trigger, or a single "add one" card when there are none.
 *
 * A separate selector rather than an extension of `selectRfNodes`: that one
 * means "the persisted graph's nodes" and several tests read it that way, so
 * widening it would be changing what it is called for.
 */
export function selectTriggerNodes(state: BuilderState): CardNode[] {
  const { at } = triggerAnchor(state);
  const triggers = state.triggers;
  const canEdit = state.env.canEdit;

  const key = [
    at.x,
    at.y,
    canEdit ? 1 : 0,
    triggers.map((t) => `${t.id}|${t.enabled ? 1 : 0}|${t.type_label}|${t.summary}`).join("~"),
  ].join("/");

  const hit = cache.get(state.triggers);
  if (hit && hit.key === key) {
    return hit.nodes;
  }

  const count = Math.max(triggers.length, 1);
  const placed = (index: number) => ({
    x: at.x - TRIGGER_DX,
    y: at.y + (index - (count - 1) / 2) * TRIGGER_DY,
  });

  // selectable:false is what keeps a synthetic id out of state.selection, and
  // so out of the inspector, the keyboard handler and the clipboard. The store
  // guards on deleteNodes/deleteEdges are the backstop behind it.
  const base = {
    draggable: false,
    selectable: false,
    deletable: false,
    connectable: false,
    focusable: false,
    width: TRIGGER_W,
    height: TRIGGER_H,
  };

  const nodes: CardNode[] =
    triggers.length === 0
      ? [
          {
            id: ADD_TRIGGER_NODE_ID,
            type: ADD_TRIGGER_NODE_TYPE,
            position: placed(0),
            data: { trigger: null, canEdit } as TriggerCardData,
            ...base,
          } as unknown as CardNode,
        ]
      : triggers.map(
          (trigger, index) =>
            ({
              id: triggerNodeId(trigger.id),
              type: TRIGGER_NODE_TYPE,
              position: placed(index),
              data: { trigger, canEdit } as TriggerCardData,
              ...base,
            }) as unknown as CardNode,
        );

  cache.set(state.triggers, { key, nodes });
  return nodes;
}

const edgeCache = new WeakMap<object, { key: string; edges: Edge[] }>();

/** The dashed lines from each card to the node that actually runs first. */
export function selectTriggerEdges(state: BuilderState): Edge[] {
  const { wired } = triggerAnchor(state);
  const key = `${wired ?? ""}/${state.triggers.map((t) => t.id).join("~")}`;

  const hit = edgeCache.get(state.triggers);
  if (hit && hit.key === key) {
    return hit.edges;
  }

  const edges: Edge[] =
    wired === null
      ? []
      : state.triggers.map((trigger) => ({
          id: `trigger-edge:${trigger.id}`,
          source: triggerNodeId(trigger.id),
          sourceHandle: "out",
          target: wired,
          type: TRIGGER_EDGE_TYPE,
          selectable: false,
          deletable: false,
          focusable: false,
          reconnectable: false,
        }));

  edgeCache.set(state.triggers, { key, edges });
  return edges;
}
