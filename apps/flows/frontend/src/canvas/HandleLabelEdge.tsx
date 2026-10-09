/**
 * An edge that says which handle it leaves by, and offers a "+" to put a step
 * on it (HANDOFF §3, Flow builder).
 *
 * The label is read from the *source node's config* through a store selector,
 * not baked into the edge, so renaming a button relabels its edge live rather
 * than at the next reload.
 *
 * The "+" is how a step gets into the middle of a flow. Before it, adding one
 * between two others meant adding it at the end, deleting an edge and drawing
 * two new ones — three precise drags for the commonest edit there is. It only
 * offers kinds of step that can carry the flow on (a step with an outgoing
 * handle), because inserting an ending would silently cut off everything after
 * it; and it only appears on edges the graph owns — the trigger card's
 * "starts here" edge is drawn by the canvas and is not one.
 *
 * On a step's only way out it is always there — that is the straight line
 * where inserting is the common edit. Where a step branches (a message with
 * five buttons) the five edges' midpoints crowd together, so there the "+"
 * appears on the edge you select.
 */
import { BaseEdge, EdgeLabelRenderer, getBezierPath, useReactFlow, type EdgeProps } from "@xyflow/react";
import { useEffect, useRef, useState } from "react";

import { nodeSpec } from "../schema/artifact";
import { handleLabel, sourceHandlesFor } from "../schema/handles";
import { newNodeConfig } from "../schema/sample";
import { useBuilder, useBuilderStore } from "../store/context";
import { StepMenu } from "./AddStep";

/** A kind of step that leaves by at least one handle, so the flow carries on. */
function carriesOn(type: string): boolean {
  const spec = nodeSpec(type);
  return Boolean(spec && !spec.annotation && sourceHandlesFor(type, newNodeConfig(type)).length > 0);
}

export function HandleLabelEdge({
  id,
  source,
  sourceHandleId,
  sourceX,
  sourceY,
  targetX,
  targetY,
  sourcePosition,
  targetPosition,
  markerEnd,
  style,
  selected,
}: EdgeProps) {
  const store = useBuilderStore();
  const config = useBuilder((state) => state.config[source]);
  const issues = useBuilder((state) => state.validation.byEdge[id]);
  const owned = useBuilder((state) => state.edge[id] !== undefined);
  const canEdit = useBuilder((state) => state.env.canEdit);
  const branches = useBuilder(
    (state) => state.edgeOrder.filter((edgeId) => state.edge[edgeId]?.source === source).length,
  );
  const [open, setOpen] = useState(false);
  const wrapper = useRef<HTMLDivElement>(null);
  const { fitView } = useReactFlow();

  useEffect(() => {
    if (!open) {
      return;
    }
    const onDown = (event: MouseEvent) => {
      if (wrapper.current && !wrapper.current.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", onDown);
    window.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      window.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const [path, labelX, labelY] = getBezierPath({
    sourceX,
    sourceY,
    targetX,
    targetY,
    sourcePosition,
    targetPosition,
  });

  const label = sourceHandleId ? handleLabel(sourceHandleId, config) : "";
  const invalid = issues?.some((issue) => issue.severity === "error");
  const insertable = canEdit && owned && (branches <= 1 || Boolean(selected) || open);

  const insert = (type: string) => {
    setOpen(false);
    const created = store.getState().insertBetween(id, type);
    if (created) {
      window.requestAnimationFrame(() => void fitView({ nodes: [{ id: created }], duration: 220, maxZoom: 1, padding: 0.6 }));
    }
  };

  return (
    <>
      <BaseEdge
        id={id}
        path={path}
        markerEnd={markerEnd}
        style={invalid ? { ...style, stroke: "var(--error-500)" } : style}
      />
      {label || invalid || insertable ? (
        <EdgeLabelRenderer>
          {label || invalid ? (
            <div
              className="fb-handle-label absolute"
              style={{
                // Above the "+" when both are drawn, so neither covers the other.
                transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY - (insertable ? 20 : 0)}px)`,
                pointerEvents: "none",
              }}
            >
              {invalid ? "! " : ""}
              {label}
            </div>
          ) : null}
          {insertable ? (
            <div
              ref={wrapper}
              className="fb-edge-insert nodrag nopan"
              style={{ transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)` }}
            >
              <button
                type="button"
                className="fb-edge-insert-button"
                aria-label="Add a step here"
                title="Add a step here"
                aria-haspopup="menu"
                aria-expanded={open}
                onClick={() => setOpen((was) => !was)}
              >
                +
              </button>
              {open ? <StepMenu className="fb-addstep-menu fb-edge-insert-menu" onPick={insert} filter={carriesOn} /> : null}
            </div>
          ) : null}
        </EdgeLabelRenderer>
      ) : null}
    </>
  );
}
