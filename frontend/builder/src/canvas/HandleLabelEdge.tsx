/**
 * An edge that says which handle it leaves by.
 *
 * The label is read from the *source node's config* through a store selector,
 * not baked into the edge, so renaming a button relabels its edge live rather
 * than at the next reload.
 */
import { BaseEdge, EdgeLabelRenderer, getBezierPath, type EdgeProps } from "@xyflow/react";

import { handleLabel } from "../schema/handles";
import { useBuilder } from "../store/context";

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
}: EdgeProps) {
  const config = useBuilder((state) => state.config[source]);
  const issues = useBuilder((state) => state.validation.byEdge[id]);

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

  return (
    <>
      <BaseEdge
        id={id}
        path={path}
        markerEnd={markerEnd}
        style={invalid ? { ...style, stroke: "var(--error-500)" } : style}
      />
      {label || invalid ? (
        <EdgeLabelRenderer>
          <div
            className="fb-handle-label absolute"
            style={{ transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)`, pointerEvents: "none" }}
          >
            {invalid ? "! " : ""}
            {label}
          </div>
        </EdgeLabelRenderer>
      ) : null}
    </>
  );
}
