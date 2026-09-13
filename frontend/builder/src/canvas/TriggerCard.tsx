/**
 * What starts the flow, as the first card on the canvas.
 *
 * A flow reads as one thing — this happens, then this, then that — and the
 * canvas used to start halfway through that sentence. The trigger lived only in
 * the left column, so the card marked "Starts here" was a step that answers
 * something, with no sign of what.
 *
 * **It is not a node, and it must never become one.** Triggers are `Trigger`
 * rows, not graph nodes: they carry a platform binding, a priority that is
 * workspace-wide, and an enabled flag, none of which a graph node has, and the
 * engine matches them long before it walks a graph. So this card is injected
 * into the *projection* in store/selectors.ts and never into `nodeType` /
 * `nodeOrder`, which is what `toGraph()` serializes. There is no code path that
 * could save it, because the maps it would have to be in are not the ones it
 * is in.
 *
 * Editing still belongs to the Django drawer — clicking this dispatches the
 * same `toggle-triggers` event the left column's button does. A second trigger
 * editor in React would be a second place for the platform gate to be wrong.
 */
import { Handle, Position as HandlePosition } from "@xyflow/react";
import { memo } from "react";

import { TRIGGER_PHRASE } from "../schema/plain";
import { useBuilder } from "../store/context";

/** The id the synthetic node and its edge are addressed by, and its React Flow type. */
export const TRIGGER_NODE_ID = "__trigger__";
export const TRIGGER_CARD_TYPE = TRIGGER_NODE_ID;

function openDrawer() {
  window.dispatchEvent(new CustomEvent("toggle-triggers", { bubbles: true }));
}

function TriggerCardInner() {
  const triggers = useBuilder((state) => state.triggers);
  const canEdit = useBuilder((state) => state.env.canEdit);
  const enabled = triggers.filter((trigger) => trigger.enabled);

  return (
    <div
      className={`fb-trigger-card${triggers.length === 0 ? " is-empty" : ""}`}
      role={canEdit ? "button" : undefined}
      tabIndex={canEdit ? 0 : undefined}
      onClick={canEdit ? openDrawer : undefined}
      onKeyDown={
        canEdit
          ? (event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                openDrawer();
              }
            }
          : undefined
      }
    >
      <p className="fb-trigger-card-eyebrow">{TRIGGER_PHRASE}</p>

      {triggers.length === 0 ? (
        <p className="fb-trigger-card-empty">
          Nothing starts this flow yet.
          {canEdit ? " Click to choose what does." : ""}
        </p>
      ) : (
        <>
          {triggers.map((trigger) => (
            <div key={trigger.id} className={trigger.enabled ? "fb-trigger-card-row" : "fb-trigger-card-row is-off"}>
              <span className="fb-trigger-card-name">{trigger.type_label}</span>
              {!trigger.enabled ? <span className="fb-trigger-card-off">Off</span> : null}
              <span className="fb-trigger-card-detail">{trigger.summary}</span>
            </div>
          ))}
          {enabled.length === 0 ? (
            <p className="fb-trigger-card-empty">All switched off, so nothing reaches this flow.</p>
          ) : null}
        </>
      )}

      {/* Source only. Nothing routes *into* what starts the flow, and
          Canvas.tsx refuses a connection at either end of this id anyway. */}
      <Handle type="source" position={HandlePosition.Right} id="starts" isConnectable={false} />
    </div>
  );
}

export const TriggerCard = memo(TriggerCardInner);
