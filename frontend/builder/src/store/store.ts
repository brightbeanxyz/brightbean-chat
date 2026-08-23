/**
 * The builder's single store.
 *
 * Two structural decisions carry most of the acceptance criteria.
 *
 * **The domain graph is the source of truth, and React Flow's objects are a
 * projection of it.** Nothing here ever holds a React Flow node. That is what
 * makes `toGraph()` able to construct rather than strip, and therefore what
 * makes round-trip fidelity a property of the design instead of a thing to
 * remember.
 *
 * **`position` is separated from the other node maps.** It changes on every
 * drag frame; `config` does not. Because the inspector subscribes to
 * `config[selectedId]` and each card subscribes to its own slices, dragging one
 * node in a hundred-node graph re-renders one card and nothing else.
 *
 * Selection and measured dimensions are view state: they never bump `revision`,
 * so clicking a node does not mark the flow dirty and does not schedule a save.
 */
import { createStore } from "zustand/vanilla";
import { subscribeWithSelector } from "zustand/middleware";

import type { BuilderEnv } from "../env";
import { LIMITS, SCHEMA_VERSION, nodeSpec } from "../schema/artifact";
import { sourceHandlesFor } from "../schema/handles";
import { newEdgeId, newNodeId, newItemId } from "../schema/ids";
import { newNodeConfig } from "../schema/sample";
import type {
  DomainEdge,
  FlowDetail,
  GraphLimits,
  Issue,
  Picklists,
  Position,
  StatsPayload,
  TriggerSummary,
  ValidationPayload,
  VersionMeta,
} from "../schema/types";
import { indexIssues, type ValidationIndex, emptyValidationIndex } from "../validation/normalize";
import { type ConfigPath, getIn, setIn, unsetIn } from "./paths";
import { type GraphState, emptyGraphState, fromGraph, sanitizePosition, toGraph } from "./serialize";

export type SaveState = "clean" | "dirty" | "saving" | "saved" | "rejected" | "error";

export interface SaveSlice {
  state: SaveState;
  version: VersionMeta | null;
  publishedVersion: VersionMeta | null;
  /** The failure the banner renders: a 422's issues, or a transport problem. */
  message: string | null;
  issues: Issue[];
}

export interface ValidationSlice extends ValidationIndex {
  /** Which `revision` this verdict describes, so a stale one can say so. */
  revision: number;
}

interface Selection {
  nodes: string[];
  edges: string[];
}

interface HistoryEntry {
  graph: GraphState;
  selection: Selection;
  key: string;
  at: number;
}

export interface ClipboardPayload {
  kind: "brightbean/flow-clip";
  schema: number;
  nodes: { id: string; type: string; position: Position; config: unknown }[];
  edges: DomainEdge[];
}

export interface BuilderState extends GraphState {
  env: BuilderEnv;
  limits: GraphLimits;

  selection: Selection;
  /** Bumped by anything that changes `toGraph()` output, and nothing else. */
  revision: number;

  past: HistoryEntry[];
  future: HistoryEntry[];
  /** True between the first frame that moves something and the drop. */
  dragging: boolean;

  picklists: Picklists;
  /** Read-only, from the flow API. The HTMX drawer on the page owns editing. */
  triggers: TriggerSummary[];
  validation: ValidationSlice;
  save: SaveSlice;
  stats: StatsPayload | null;
  /** Separate from `stats` so a broken endpoint cannot read as "no data yet". */
  statsFailed: boolean;
  statsVisible: boolean;
  loaded: boolean;

  /** Replace the read-only trigger list after a drawer edit or a tab refocus. */
  setTriggers: (triggers: TriggerSummary[]) => void;

  // ── graph mutations ───────────────────────────────────────────────────────
  load: (detail: FlowDetail) => void;
  addNode: (type: string, position: Position, options?: { cascade?: boolean }) => string | null;
  updateConfig: (nodeId: string, path: ConfigPath, value: unknown, historyKey?: string) => void;
  clearConfig: (nodeId: string, path: ConfigPath) => void;
  replaceConfig: (nodeId: string, config: unknown, historyKey?: string) => void;
  moveNodes: (moves: readonly { id: string; position: Position }[]) => void;
  beginDrag: () => void;
  endDrag: () => void;
  deleteNodes: (ids: string[]) => void;
  deleteEdges: (ids: string[]) => void;
  connect: (source: string, sourceHandle: string, target: string) => void;
  paste: (payload: ClipboardPayload, at?: Position) => void;

  // ── view ──────────────────────────────────────────────────────────────────
  setSelection: (selection: Selection) => void;
  toggleStats: () => void;

  // ── history ───────────────────────────────────────────────────────────────
  undo: () => void;
  redo: () => void;

  // ── server-derived ────────────────────────────────────────────────────────
  applyValidation: (payload: ValidationPayload, revision: number) => void;
  setSave: (patch: Partial<SaveSlice>) => void;
  setStats: (stats: StatsPayload | null) => void;
  setStatsFailed: (failed: boolean) => void;
}

export const EMPTY_PICKLISTS: Picklists = {
  tags: [],
  custom_fields: [],
  sequences: [],
  flows: [],
  connections: [],
  members: [],
};

/** Entries kept, and how long two edits may coalesce into one. */
const HISTORY_LIMIT = 50;
const COALESCE_MS = 500;

function graphOf(state: BuilderState): GraphState {
  return {
    nodeOrder: state.nodeOrder,
    nodeType: state.nodeType,
    config: state.config,
    position: state.position,
    edgeOrder: state.edgeOrder,
    edge: state.edge,
    schemaVersion: state.schemaVersion,
  };
}

function omit<T>(record: Record<string, T>, keys: Set<string>): Record<string, T> {
  const out: Record<string, T> = {};
  for (const [key, value] of Object.entries(record)) {
    if (!keys.has(key)) {
      out[key] = value;
    }
  }
  return out;
}

export type BuilderStore = ReturnType<typeof createBuilderStore>;

export function createBuilderStore(env: BuilderEnv) {
  return createStore<BuilderState>()(
    subscribeWithSelector((set, get) => {
      /**
       * Snapshot, then mutate.
       *
       * Snapshots rather than inverse patches: the store is normalised and
       * immutable, so an entry shares structure with its predecessor and costs
       * O(changed) — while inverse-patch bookkeeping over arbitrary user JSON
       * edited by a generated form is where the bugs live.
       *
       * `key` coalesces: typing a subject line is one undo step, not forty.
       */
      // When the previous edit happened, which is not the same as when the
      // previous history entry was pushed. Measuring from the entry never
      // slides the window, so a continuous burst of typing still accumulates a
      // step every COALESCE_MS instead of collapsing into one.
      let lastEditAt = 0;
      let lastEditKey: string | null = null;

      const withHistory = (key: string, mutate: (state: BuilderState) => Partial<BuilderState>) => {
        const before = get();
        const now = Date.now();
        const entry: HistoryEntry = { graph: graphOf(before), selection: before.selection, key, at: now };

        const continues = lastEditKey === key && now - lastEditAt < COALESCE_MS && before.past.length > 0;
        lastEditAt = now;
        lastEditKey = key;

        const past = continues ? before.past : [...before.past, entry].slice(-HISTORY_LIMIT);
        set({ ...mutate(before), past, future: [], revision: before.revision + 1 });
      };

      const restore = (entry: HistoryEntry, counterpart: HistoryEntry[], stack: "past" | "future") =>
        set((state) => ({
          ...entry.graph,
          selection: entry.selection,
          revision: state.revision + 1,
          [stack]: counterpart,
        }));

      return {
        ...emptyGraphState(),
        env,
        limits: LIMITS,

        selection: { nodes: [], edges: [] },
        revision: 0,

        past: [],
        future: [],
        dragging: false,

        picklists: EMPTY_PICKLISTS,
        validation: { ...emptyValidationIndex(), revision: 0 },
        save: { state: "clean", version: null, publishedVersion: null, message: null, issues: [] },
        triggers: [],
        stats: null,
        statsFailed: false,
        statsVisible: false,
        loaded: false,

        load: (detail) =>
          set((state) => ({
            ...fromGraph(detail.graph),
            picklists: detail.picklists,
            triggers: detail.triggers,
            limits: detail.limits,
            validation: { ...indexIssues(detail.validation), revision: state.revision },
            save: {
              state: "clean",
              version: detail.version,
              publishedVersion: detail.published_version,
              message: null,
              issues: [],
            },
            past: [],
            future: [],
            selection: { nodes: [], edges: [] },
            loaded: true,
          })),

        setTriggers: (triggers) => set({ triggers }),

        addNode: (type, position, options) => {
          const state = get();
          if (state.nodeOrder.length >= state.limits.max_nodes) {
            return null;
          }
          const id = newNodeId();
          // Cascade only when the caller had no particular spot in mind. Every
          // click on a palette item targets the same pane centre, so without it
          // the second click hides under the first — but a drop's coordinates
          // are the reader's explicit choice and must be left alone.
          const at = options?.cascade ? freePositionNear(position, Object.values(state.position)) : position;
          withHistory(`add:${id}`, (before) => ({
            nodeOrder: [...before.nodeOrder, id],
            nodeType: { ...before.nodeType, [id]: type },
            config: { ...before.config, [id]: newNodeConfig(type) },
            position: { ...before.position, [id]: sanitizePosition(at) },
            selection: { nodes: [id], edges: [] },
          }));
          return id;
        },

        updateConfig: (nodeId, path, value, historyKey) =>
          withHistory(historyKey ?? `config:${nodeId}:${path.join(".")}`, (before) => ({
            config: { ...before.config, [nodeId]: setIn(before.config[nodeId], path, value) },
          })),

        clearConfig: (nodeId, path) =>
          withHistory(`clear:${nodeId}:${path.join(".")}`, (before) => ({
            config: { ...before.config, [nodeId]: unsetIn(before.config[nodeId], path) },
          })),

        replaceConfig: (nodeId, config, historyKey) =>
          withHistory(historyKey ?? `replace:${nodeId}`, (before) => ({
            config: { ...before.config, [nodeId]: config },
          })),

        // Per-frame during a drag: writes positions and nothing else. No
        // history entry, no revision bump — endDrag() does both, once.
        //
        // Takes the whole batch rather than one node, because React Flow emits
        // one change per selected node per frame. One `set` per node would mean
        // one store notification per node, and every notification re-projects
        // the entire graph — dragging a 50-node selection in a 100-node flow
        // would cost 5000 projection steps a frame instead of 100.
        moveNodes: (moves) => {
          if (moves.length === 0) {
            return;
          }
          set((state) => {
            const position = { ...state.position };
            for (const move of moves) {
              position[move.id] = sanitizePosition(move.position);
            }
            return { position };
          });
        },

        // Idempotent, because React Flow reports `dragging: true` on every
        // frame of a drag, not once at the start. Pushing an entry per frame
        // makes Undo step back a single frame and lets one drag consume the
        // whole 50-entry history.
        beginDrag: () => {
          if (get().dragging) {
            return;
          }
          lastEditKey = null;
          set((state) => ({
            dragging: true,
            past: [
              ...state.past,
              { graph: graphOf(state), selection: state.selection, key: `move:${Date.now()}`, at: Date.now() },
            ].slice(-HISTORY_LIMIT),
            future: [],
          }));
        },

        endDrag: () =>
          set((state) => (state.dragging ? { dragging: false, revision: state.revision + 1 } : {})),

        deleteNodes: (ids) => {
          const doomed = new Set(ids);
          if (doomed.size === 0) {
            return;
          }
          withHistory(`delete:${[...doomed].join(",")}`, (before) => {
            // Incident edges go with the nodes. Leaving them behind is a pile
            // of `dangling_edge` on the very next save.
            const orphaned = new Set(
              before.edgeOrder.filter((edgeId) => {
                const edge = before.edge[edgeId];
                return edge !== undefined && (doomed.has(edge.source) || doomed.has(edge.target));
              }),
            );
            return {
              nodeOrder: before.nodeOrder.filter((id) => !doomed.has(id)),
              nodeType: omit(before.nodeType, doomed),
              config: omit(before.config, doomed),
              position: omit(before.position, doomed),
              edgeOrder: before.edgeOrder.filter((id) => !orphaned.has(id)),
              edge: omit(before.edge, orphaned),
              selection: { nodes: [], edges: [] },
            };
          });
        },

        deleteEdges: (ids) => {
          const doomed = new Set(ids);
          if (doomed.size === 0) {
            return;
          }
          withHistory(`delete-edges:${[...doomed].join(",")}`, (before) => ({
            edgeOrder: before.edgeOrder.filter((id) => !doomed.has(id)),
            edge: omit(before.edge, doomed),
            selection: { nodes: before.selection.nodes, edges: [] },
          }));
        },

        connect: (source, sourceHandle, target) => {
          const state = get();
          if (state.edgeOrder.length >= state.limits.max_edges) {
            return;
          }
          const id = newEdgeId();
          withHistory(`connect:${id}`, (before) => {
            // One edge per (source, handle): a second is `duplicate_handle_edge`,
            // so re-connecting a used handle replaces rather than stacks.
            const superseded = new Set(
              before.edgeOrder.filter((edgeId) => {
                const edge = before.edge[edgeId];
                return edge !== undefined && edge.source === source && edge.sourceHandle === sourceHandle;
              }),
            );
            return {
              edgeOrder: [...before.edgeOrder.filter((edgeId) => !superseded.has(edgeId)), id],
              edge: { ...omit(before.edge, superseded), [id]: { id, source, sourceHandle, target } },
            };
          });
        },

        paste: (payload, at) => {
          const state = get();
          if (payload.kind !== "brightbean/flow-clip" || payload.nodes.length === 0) {
            return;
          }
          if (state.nodeOrder.length + payload.nodes.length > state.limits.max_nodes) {
            return;
          }

          const idMap = new Map<string, string>();
          const handleMap = new Map<string, string>();
          const nodes = payload.nodes.map((node) => {
            const id = newNodeId();
            idMap.set(node.id, id);
            return { id, type: node.type, config: remintItemIds(node, handleMap), position: node.position };
          });

          const origin = payload.nodes.reduce(
            (best, node) => ({ x: Math.min(best.x, node.position.x), y: Math.min(best.y, node.position.y) }),
            { x: Infinity, y: Infinity },
          );
          const offset = at ? { x: at.x - origin.x, y: at.y - origin.y } : { x: 40, y: 40 };

          const kept = payload.edges.filter((edge) => idMap.has(edge.source) && idMap.has(edge.target));
          // The edge cap is as real as the node cap: overshooting it makes the
          // server reject every autosave until someone deletes edges by hand.
          if (state.edgeOrder.length + kept.length > state.limits.max_edges) {
            return;
          }
          const edges = kept
            .map((edge) => ({
              id: newEdgeId(),
              source: idMap.get(edge.source) as string,
              sourceHandle: handleMap.get(`${edge.source}|${edge.sourceHandle}`) ?? edge.sourceHandle,
              target: idMap.get(edge.target) as string,
            }));

          withHistory(`paste:${nodes[0]?.id ?? ""}`, (before) => ({
            nodeOrder: [...before.nodeOrder, ...nodes.map((node) => node.id)],
            nodeType: { ...before.nodeType, ...Object.fromEntries(nodes.map((n) => [n.id, n.type])) },
            config: { ...before.config, ...Object.fromEntries(nodes.map((n) => [n.id, n.config])) },
            position: {
              ...before.position,
              ...Object.fromEntries(
                nodes.map((n) => [n.id, sanitizePosition({ x: n.position.x + offset.x, y: n.position.y + offset.y })]),
              ),
            },
            edgeOrder: [...before.edgeOrder, ...edges.map((edge) => edge.id)],
            edge: { ...before.edge, ...Object.fromEntries(edges.map((edge) => [edge.id, edge])) },
            selection: { nodes: nodes.map((node) => node.id), edges: [] },
          }));
        },

        setSelection: (selection) => set({ selection }),

        toggleStats: () => set((state) => ({ statsVisible: !state.statsVisible })),

        undo: () => {
          lastEditKey = null;
          const state = get();
          const entry = state.past[state.past.length - 1];
          if (!entry) {
            return;
          }
          set({ future: [...state.future, { graph: graphOf(state), selection: state.selection, key: "redo", at: Date.now() }] });
          restore(entry, state.past.slice(0, -1), "past");
        },

        redo: () => {
          lastEditKey = null;
          const state = get();
          const entry = state.future[state.future.length - 1];
          if (!entry) {
            return;
          }
          set({ past: [...state.past, { graph: graphOf(state), selection: state.selection, key: "undo", at: Date.now() }] });
          restore(entry, state.future.slice(0, -1), "future");
        },

        applyValidation: (payload, revision) => set({ validation: { ...indexIssues(payload), revision } }),

        setSave: (patch) => set((state) => ({ save: { ...state.save, ...patch } })),

        setStats: (stats) => set({ stats, statsFailed: false }),

        setStatsFailed: (statsFailed) => set({ statsFailed }),
      };
    }),
  );
}

/** How close two cards may sit before the new one is nudged, in flow units. */
const OVERLAP = 24;
const CASCADE = 36;

/**
 * A spot near `at` that no card already occupies.
 *
 * Adding from the palette by click places at the centre of the pane, so two
 * clicks in a row would otherwise stack one card exactly on another and the
 * second would look like nothing happened. Cascading is what every canvas
 * editor does, and it costs one loop.
 */
export function freePositionNear(at: Position, taken: readonly Position[]): Position {
  let candidate = { ...at };
  for (let step = 0; step < 200; step += 1) {
    const clash = taken.some(
      (other) => Math.abs(other.x - candidate.x) < OVERLAP && Math.abs(other.y - candidate.y) < OVERLAP,
    );
    if (!clash) {
      return candidate;
    }
    candidate = { x: candidate.x + CASCADE, y: candidate.y + CASCADE };
  }
  return candidate;
}

/**
 * Give every config item that backs a dynamic handle a fresh id, recording the
 * old→new handle mapping so the pasted edges can be rewritten.
 *
 * Driven by the artefact's `dynamic_handles`, so `btn`, `qr` and `rand` are
 * never named in this file — a prefix added by a later layer is handled here
 * with no edit. The server only requires item ids to be unique *within* a node,
 * so a straight copy would not collide today; reminting anyway costs nothing,
 * makes same-flow and cross-flow paste one code path, and removes a dependence
 * on an invariant the schema does not actually state.
 */
function remintItemIds(
  node: { id: string; type: string; config: unknown },
  handleMap: Map<string, string>,
): unknown {
  const spec = nodeSpec(node.type);
  if (!spec || spec.dynamic_handles.length === 0 || typeof node.config !== "object" || node.config === null) {
    return node.config;
  }

  let config: unknown = node.config;
  for (const { prefix, config_key } of spec.dynamic_handles) {
    const items = getIn(config, [config_key]);
    if (!Array.isArray(items)) {
      continue;
    }
    items.forEach((item, index) => {
      if (typeof item !== "object" || item === null || typeof (item as Record<string, unknown>)["id"] !== "string") {
        return;
      }
      const previous = (item as Record<string, unknown>)["id"] as string;
      const next = newItemId();
      handleMap.set(`${node.id}|${prefix}:${previous}`, `${prefix}:${next}`);
      config = setIn(config, [config_key, index, "id"], next);
    });
  }
  return config;
}

/** The clipboard payload for a selection, ready to serialise. */
export function clipboardFor(state: BuilderState, nodeIds: string[]): ClipboardPayload {
  const selected = new Set(nodeIds);
  return {
    kind: "brightbean/flow-clip",
    schema: SCHEMA_VERSION,
    nodes: state.nodeOrder
      .filter((id) => selected.has(id))
      .map((id) => ({
        id,
        type: state.nodeType[id] as string,
        position: state.position[id] ?? { x: 0, y: 0 },
        config: state.config[id],
      })),
    // Only edges wholly inside the selection: a half-copied edge pastes as a
    // dangling one.
    edges: state.edgeOrder
      .map((id) => state.edge[id])
      .filter((edge): edge is DomainEdge => edge !== undefined && selected.has(edge.source) && selected.has(edge.target)),
  };
}

/** Handles this node exposes right now, for the card and for edge validation. */
export function handlesOf(state: BuilderState, nodeId: string): string[] {
  const type = state.nodeType[nodeId];
  return type === undefined ? [] : sourceHandlesFor(type, state.config[nodeId]);
}

export { toGraph, graphOf };
