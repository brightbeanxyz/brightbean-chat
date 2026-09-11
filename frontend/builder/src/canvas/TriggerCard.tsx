/**
 * What starts this flow, on the canvas where the flow is.
 *
 * A distinct component rather than a variant of FlowNodeCard: that one
 * subscribes to `nodeType`, `config`, `validation.byNode`, `selectEntryIds` and
 * `stats.nodes` for its own id, every one of which is undefined for a synthetic
 * id — its first guard would blank the card. Retrofitting it means five
 * `if (synthetic)` branches in the hottest component in the bundle, to save a
 * component that subscribes to nothing.
 */
import { Handle, Position as HandlePosition } from "@xyflow/react";

import { openTriggerDrawer } from "./triggerEvents";
import type { TriggerCardData } from "./triggerNodes";

export function TriggerCard({ data }: { data: TriggerCardData }) {
  const trigger = data.trigger;
  if (!trigger) {
    return null;
  }

  return (
    <button
      type="button"
      className={["fb-trigger", trigger.enabled ? "" : "fb-trigger-paused"].filter(Boolean).join(" ")}
      // React Flow's own class hooks. Without them a mousedown on this button
      // starts a pane drag on some pointer paths.
      onClick={() => openTriggerDrawer(trigger.id)}
      data-trigger-id={trigger.id}
      title="Open this trigger"
    >
      <div className="fb-trigger-header">
        <span className="truncate">{trigger.type_label}</span>
        {!trigger.enabled ? <span className="fb-trigger-flag ml-auto">OFF</span> : null}
      </div>
      <div className="fb-trigger-body">{trigger.summary}</div>
      {trigger.connection ? <div className="fb-trigger-foot">{trigger.connection.label}</div> : null}
      <Handle type="source" position={HandlePosition.Right} id="out" isConnectable={false} />
    </button>
  );
}

/**
 * The empty state, in the place the missing thing would be.
 *
 * A viewer gets the same card as a statement rather than a disabled button:
 * they cannot add a trigger, and "this will not run" is the useful half of that
 * anyway.
 */
export function AddTriggerCard({ data }: { data: TriggerCardData }) {
  if (!data.canEdit) {
    return (
      <div className="fb-trigger fb-trigger-add">
        <div className="fb-trigger-body">No triggers — this flow will not run when published.</div>
      </div>
    );
  }
  return (
    <button
      type="button"
      className="fb-trigger fb-trigger-add nodrag nopan"
      onClick={() => openTriggerDrawer(null)}
    >
      <div className="fb-trigger-header">Add a trigger</div>
      <div className="fb-trigger-body">Nothing starts this flow yet, so publishing it would run nothing.</div>
    </button>
  );
}
