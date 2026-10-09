/**
 * The outline: the builder's left column (HANDOFF §3, Flow builder).
 *
 * Two lists that answer the two questions a flow is made of — what starts it,
 * and what it does, in order. It replaced a trigger column on the left and a
 * step list stacked inside the inspector on the right: the steps are an index
 * of the canvas, and an index belongs beside the map rather than above the
 * settings of one step.
 */
import { useBuilder } from "../store/context";
import { StepList } from "./StepList";
import { TriggerSection } from "./TriggerSection";

export function TriggerSidebar() {
  const triggerCount = useBuilder((state) => state.triggers.length);
  const stepCount = useBuilder((state) => state.nodeOrder.length);

  return (
    <aside className="fb-trigger-sidebar" aria-label="Outline">
      <TriggerSection />
      {triggerCount === 0 && stepCount === 0 ? (
        <section className="fb-editor-start">
          <p className="fb-editor-start-title">Two things make a flow</p>
          <p className="fb-editor-start-body">
            Something that starts it, and something it does. Choose what starts it here, then add
            your first step on the canvas.
          </p>
        </section>
      ) : null}
      {stepCount > 0 ? (
        <section className="fb-outline-steps">
          <h2 className="fb-trigger-heading">Steps</h2>
          <StepList />
        </section>
      ) : null}
    </aside>
  );
}
