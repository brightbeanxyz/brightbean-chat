/**
 * The one node component. Every type in the artefact renders through it.
 *
 * It subscribes to its own slices — this node's config, this node's issues,
 * this node's stats — rather than receiving them through `data`. That is the
 * whole reason a hundred-node graph stays responsive: dragging one node writes
 * one entry in `position`, so one card re-renders and the other ninety-nine,
 * plus the inspector, do not.
 */
import { Handle, Position as HandlePosition, useUpdateNodeInternals } from "@xyflow/react";
import { memo, useEffect } from "react";

import { groupOf, nodeSpec } from "../schema/artifact";
import { handleLabel, sourceHandles } from "../schema/handles";
import { useBuilder } from "../store/context";
import { selectEntryIds, type CardData } from "../store/selectors";
import { worstSeverity } from "../validation/normalize";
import { NodePreview } from "./previews";

const HANDLE_COPY: Record<string, string> = { default: "Next" };

function FlowNodeCardInner({ id, data, selected }: { id: string; data: CardData; selected?: boolean }) {
  const nodeId = data.nodeId;
  const type = useBuilder((state) => state.nodeType[nodeId]);
  const config = useBuilder((state) => state.config[nodeId]);
  const picklists = useBuilder((state) => state.picklists);
  const issues = useBuilder((state) => state.validation.byNode[nodeId]);
  const isEntry = useBuilder((state) => selectEntryIds(state).has(nodeId));
  const stats = useBuilder((state) => (state.statsVisible ? state.stats : null));

  const spec = type === undefined ? undefined : nodeSpec(type);
  const handles = spec ? sourceHandles(spec, config) : [];
  const severity = worstSeverity(issues);

  // React Flow caches each handle's measured bounds. Adding a button changes
  // the handle set without changing the node's identity, so without this the
  // new handle exists in the DOM while edges still attach to stale coordinates.
  const updateNodeInternals = useUpdateNodeInternals();
  const handleKey = handles.join("|");
  useEffect(() => {
    updateNodeInternals(id);
  }, [id, handleKey, updateNodeInternals]);

  if (type === undefined) {
    return null;
  }

  const group = spec ? groupOf(spec) : "other";
  const isNote = Boolean(spec?.annotation);
  const nodeStats = stats?.nodes[nodeId];

  return (
    <div
      className={[
        "fb-node",
        `fb-node-${group}`,
        isNote ? "fb-node-note" : "",
        selected ? "is-selected" : "",
        severity === "error" ? "is-invalid" : "",
      ]
        .filter(Boolean)
        .join(" ")}
      data-node-type={type}
      data-group={group}
    >
      {/* A note takes part in no routing at all (SPEC §11.11), so it gets no
          handles — which is also what stops `note_node_connected`. */}
      {!isNote ? <Handle type="target" position={HandlePosition.Left} /> : null}

      <div className="fb-node-header">
        <span className="truncate">{spec?.label ?? type}</span>
        {isEntry && !isNote ? <span className="fb-entry-flag ml-auto">START</span> : null}
      </div>

      <div className="fb-node-body">
        <NodePreview type={type} config={config} picklists={picklists} />
      </div>

      {severity || nodeStats ? (
        <div className="fb-node-footer">
          {severity ? (
            <span className={`fb-badge fb-badge-${severity}`}>
              {issues?.length} {severity === "error" ? "error" : "warning"}
              {(issues?.length ?? 0) === 1 ? "" : "s"}
            </span>
          ) : null}
          {nodeStats ? (
            <span className="fb-pill" title="Sent · delivered · failed">
              {nodeStats.sent} · {nodeStats.delivered} · {nodeStats.failed}
            </span>
          ) : stats && !stats.available && !isNote ? (
            <span className="fb-pill">—</span>
          ) : null}
        </div>
      ) : null}

      {handles.map((handle, index) => (
        <Handle
          key={handle}
          id={handle}
          type="source"
          position={HandlePosition.Right}
          // Spread evenly down the right edge so a send_message with six
          // buttons is still readable.
          style={{ top: `${((index + 1) / (handles.length + 1)) * 100}%` }}
        >
          <span className="fb-handle-label absolute left-3 -translate-y-1/2 pointer-events-none whitespace-nowrap">
            {HANDLE_COPY[handle] ?? handleLabel(handle, config)}
          </span>
        </Handle>
      ))}
    </div>
  );
}

export const FlowNodeCard = memo(FlowNodeCardInner);
