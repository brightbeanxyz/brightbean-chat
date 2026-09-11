/**
 * The line from a trigger card to the node that runs first.
 *
 * Dashed, and that is not decoration: it reads as "not part of the graph you
 * are saving", which is literally true — this edge is derived from the trigger
 * list and never reaches `toGraph()`.
 *
 * Subscribes to nothing.
 */
import { BaseEdge, getBezierPath, type EdgeProps } from "@xyflow/react";

export function TriggerEdge({
  sourceX,
  sourceY,
  targetX,
  targetY,
  sourcePosition,
  targetPosition,
  markerEnd,
}: EdgeProps) {
  const [path] = getBezierPath({ sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition });

  return <BaseEdge path={path} markerEnd={markerEnd} className="fb-trigger-edge" />;
}
