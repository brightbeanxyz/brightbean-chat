/**
 * The React Flow pane.
 *
 * The interesting part is `onNodesChange`, which demultiplexes by change type
 * rather than handing the whole array to `applyNodeChanges`. Each kind goes
 * somewhere different, and three of the four deliberately do not touch
 * `revision`:
 *
 * | change       | destination                                              |
 * |--------------|----------------------------------------------------------|
 * | `position`   | `position[id]`; revision bumped once, on drag end        |
 * | `select`     | `selection` only — a click must not mark the flow dirty  |
 * | `dimensions` | discarded; `measured`/`width`/`height` are pure view state|
 * | `remove`     | routed to `deleteNodes` so incident edges go too         |
 */
import {
  Background,
  Controls,
  MiniMap,
  ReactFlow,
  useReactFlow,
  type Connection,
  type EdgeChange,
  type IsValidConnection,
  type NodeChange,
} from "@xyflow/react";
import { useCallback, useMemo, useRef } from "react";

import { groupOf, nodeSpec } from "../schema/artifact";
import type { Position } from "../schema/types";
import { useBuilder, useBuilderStore } from "../store/context";
import { selectRfEdges, selectRfNodes, type CardData, type CardNode } from "../store/selectors";
import { AddStep } from "./AddStep";
import { TRIGGER_NODE_ID } from "./TriggerCard";
import { edgeTypes, nodeTypes } from "./types";
import { useKeyboard } from "./useKeyboard";

/**
 * How big the minimap is, in pixels.
 *
 * React Flow's default is 200x150, which on a three-step flow is a white
 * rectangle taking a corner of the canvas to show three dots. It cannot be
 * sized from CSS: `MiniMap` reads `style.width` and `style.height` as **numbers**
 * and divides the graph's bounding box by them, so a `.react-flow__minimap`
 * rule (which the conventions forbid anyway — React Flow's own stylesheet is
 * unlayered and loads after ours) would resize the box and leave the viewport
 * mask computed against the old one.
 *
 * Numbers, therefore, and not "120px": the arithmetic gives NaN on a string and
 * the mask silently disappears.
 */
const MINIMAP = { width: 132, height: 92 } as const;

const GROUP_COLOR: Record<string, string> = {
  content: "var(--flow-group-content)",
  logic: "var(--flow-group-logic)",
  actions: "var(--flow-group-actions)",
  other: "var(--flow-group-other)",
};

export function Canvas() {
  const store = useBuilderStore();
  const nodes = useBuilder(selectRfNodes);
  const edges = useBuilder(selectRfEdges);
  const canEdit = useBuilder((state) => state.env.canEdit);
  const { screenToFlowPosition } = useReactFlow();
  const wrapper = useRef<HTMLDivElement>(null);

  useKeyboard();

  const onNodesChange = useCallback(
    (changes: NodeChange<CardNode>[]) => {
      const state = store.getState();
      let selection: string[] | null = null;
      const moves: { id: string; position: Position }[] = [];
      const sized: { id: string; width: number; height: number }[] = [];
      const removed: string[] = [];
      let dragEnded = false;
      let dragging = false;

      for (const change of changes) {
        // The trigger card is drawn on the canvas and is not in the graph, so
        // a move, a selection or a removal of it names something the store does
        // not have: `deleteNodes(["__trigger__"])` is a silent no-op today and a
        // corrupted map the day deleteNodes stops checking.
        //
        // Its *measurement* is the exception, and has to be, because `measured`
        // is what puts a node inside Fit's bounds — dropping it here is what
        // left the card off screen after pressing Fit.
        if ("id" in change && change.id === TRIGGER_NODE_ID && change.type !== "dimensions") {
          continue;
        }
        switch (change.type) {
          case "position": {
            if (change.position) {
              moves.push({ id: change.id, position: change.position });
            }
            if (change.dragging === false) {
              dragEnded = true;
            } else if (change.dragging) {
              dragging = true;
            }
            break;
          }
          case "select": {
            selection ??= [...store.getState().selection.nodes];
            selection = change.selected
              ? [...new Set([...selection, change.id])]
              : selection.filter((id) => id !== change.id);
            break;
          }
          case "remove": {
            removed.push(change.id);
            break;
          }
          case "dimensions": {
            // Kept, in a view-only slice. These used to be discarded along with
            // `replace` and `add`, on the grounds that they are not ours and the
            // server rejects them — both true, and it cost more than it saved:
            // React Flow computes every node box from `measured`, so without it
            // Fit found no bounds and did nothing, and the minimap drew nothing.
            // `setMeasured` touches neither `revision` nor history.
            if (change.dimensions) {
              sized.push({ id: change.id, ...change.dimensions });
            }
            break;
          }
          default:
            // `replace` and `add` — internals we do not own and have no use for.
            break;
        }
      }

      if (sized.length > 0) {
        state.setMeasured(sized);
      }
      if (moves.length > 0) {
        // On the first frame that actually moves something, not on drag start:
        // a click-and-hold that never moves would otherwise leave a no-op step
        // on the undo stack and discard the redo stack with it. `dragging` is
        // reported on every frame, so beginDrag() is idempotent for a drag.
        if (dragging) {
          state.beginDrag();
        }
        state.moveNodes(moves);
      }
      if (selection !== null) {
        store.getState().setSelection({ nodes: selection, edges: store.getState().selection.edges });
      }
      if (dragEnded) {
        store.getState().endDrag();
      }
      if (removed.length > 0 && canEdit) {
        store.getState().deleteNodes(removed);
      }
    },
    [store, canEdit],
  );

  const onEdgesChange = useCallback(
    (changes: EdgeChange[]) => {
      const removed: string[] = [];
      // Accumulated from the current selection, exactly like nodes. Collecting
      // only `selected: true` and skipping an empty result meant a deselection
      // never reached the store, so an edge could not be unselected once
      // clicked — and a later Delete would still take it.
      let selection: string[] | null = null;

      for (const change of changes) {
        if ("id" in change && change.id.startsWith(`${TRIGGER_NODE_ID}-`)) {
          continue;
        }
        if (change.type === "remove") {
          removed.push(change.id);
        } else if (change.type === "select") {
          selection ??= [...store.getState().selection.edges];
          selection = change.selected
            ? [...new Set([...selection, change.id])]
            : selection.filter((id) => id !== change.id);
        }
      }

      if (selection !== null) {
        store.getState().setSelection({ nodes: store.getState().selection.nodes, edges: selection });
      }
      if (removed.length > 0 && canEdit) {
        store.getState().deleteEdges(removed);
      }
    },
    [store, canEdit],
  );

  const onConnect = useCallback(
    (connection: Connection) => {
      if (!canEdit || !connection.source || !connection.target) {
        return;
      }
      store.getState().connect(connection.source, connection.sourceHandle ?? "default", connection.target);
    },
    [store, canEdit],
  );

  /**
   * Refuse the connections the server would reject anyway, so the user finds
   * out while dragging rather than from a badge two seconds later. Cycles are
   * NOT refused: SPEC §9.1 allows them and the runtime's loop cap is what
   * protects against them.
   */
  const isValidConnection = useCallback<IsValidConnection>(
    (connection) => {
      // Nothing connects to or from the trigger card. Its one edge is drawn by
      // the projection, and a second would imply a second entry step.
      if (connection.source === TRIGGER_NODE_ID || connection.target === TRIGGER_NODE_ID) {
        return false;
      }
      const state = store.getState();
      const sourceType = connection.source ? state.nodeType[connection.source] : undefined;
      const targetType = connection.target ? state.nodeType[connection.target] : undefined;
      const sourceSpec = sourceType === undefined ? undefined : nodeSpec(sourceType);
      const targetSpec = targetType === undefined ? undefined : nodeSpec(targetType);

      // A note may not be an edge endpoint at all (`note_node_connected`), and
      // a terminal node ends the execution in-graph so an edge out of it is
      // unreachable (`terminal_node_has_outgoing_edge`).
      if (sourceSpec?.annotation || targetSpec?.annotation || sourceSpec?.terminal) {
        return false;
      }
      return true;
    },
    [store],
  );

  const onDrop = useCallback(
    (event: React.DragEvent) => {
      event.preventDefault();
      const type = event.dataTransfer.getData("application/x-brightbean-node");
      if (!type || !canEdit) {
        return;
      }
      store.getState().addNode(type, screenToFlowPosition({ x: event.clientX, y: event.clientY }));
    },
    [store, canEdit, screenToFlowPosition],
  );

  const minimapColor = useMemo(
    () => (node: CardNode) => {
      const spec = node.type ? nodeSpec(node.type) : undefined;
      return GROUP_COLOR[spec ? groupOf(spec) : "other"] ?? GROUP_COLOR["other"] ?? "var(--border-hover)";
    },
    [],
  );

  return (
    <div
      ref={wrapper}
      className="fb-canvas flex-1 min-h-0 relative"
      onDrop={onDrop}
      onDragOver={(event) => event.preventDefault()}
    >
      <ReactFlow<CardNode>
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        edgeTypes={edgeTypes}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onConnect={onConnect}
        isValidConnection={isValidConnection}
        nodesDraggable={canEdit}
        nodesConnectable={canEdit}
        elementsSelectable
        edgesReconnectable={false}
        // Handled by useKeyboard so a Backspace typed into a panel input never
        // deletes the node being edited.
        deleteKeyCode={null}
        selectionKeyCode="Shift"
        multiSelectionKeyCode={["Meta", "Control"]}
        fitView
        proOptions={{ hideAttribution: false }}
      >
        <Background />
        <Controls showInteractive={false} position="bottom-right" />
        <MiniMap<CardNode>
          pannable
          zoomable
          nodeColor={minimapColor}
          position="top-right"
          style={{ ...MINIMAP, borderRadius: "var(--radius-md)", border: "1px solid var(--border)" }}
        />
      </ReactFlow>
      {/* Outside <ReactFlow> so it is not pan/zoom transformed, and after it so
          it stacks above the pane without a z-index fight. */}
      <AddStep />
    </div>
  );
}

export type { CardData };
