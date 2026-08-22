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
import { useBuilder, useBuilderStore } from "../store/context";
import { selectRfEdges, selectRfNodes, type CardData, type CardNode } from "../store/selectors";
import { edgeTypes, nodeTypes } from "./types";
import { useKeyboard } from "./useKeyboard";

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
      const removed: string[] = [];
      let dragEnded = false;

      for (const change of changes) {
        switch (change.type) {
          case "position": {
            if (change.position) {
              state.moveNode(change.id, change.position);
            }
            if (change.dragging === false) {
              dragEnded = true;
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
          default:
            // `dimensions`, `replace`, `add` — measurement and internals we do
            // not own. Discarded, because they are exactly the keys the server
            // rejects and there is no reason to carry them.
            break;
        }
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
      const removed = changes.flatMap((change) => (change.type === "remove" ? [change.id] : []));
      const selection = changes.flatMap((change) =>
        change.type === "select" && change.selected ? [change.id] : [],
      );
      if (selection.length > 0) {
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
    <div ref={wrapper} className="flex-1 min-h-0" onDrop={onDrop} onDragOver={(event) => event.preventDefault()}>
      <ReactFlow<CardNode>
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        edgeTypes={edgeTypes}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onConnect={onConnect}
        isValidConnection={isValidConnection}
        onNodeDragStart={() => store.getState().beginDrag()}
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
        <Controls showInteractive={false} />
        <MiniMap<CardNode> pannable zoomable nodeColor={minimapColor} />
      </ReactFlow>
    </div>
  );
}

export type { CardData };
