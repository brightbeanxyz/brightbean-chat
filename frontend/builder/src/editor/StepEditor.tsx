/**
 * The left column: what starts this flow, its steps, and the selected step's settings.
 *
 * This replaces the palette and the inspector, which were two panels either
 * side of the canvas doing one job between them — you picked a node type on the
 * left, it landed in the middle, and you configured it on the right, with your
 * eye crossing the window twice per step. Steps are added from the canvas now
 * (see AddStep), so the left column is the one place a step is read and edited.
 *
 * Three states, in order of what the reader needs:
 *
 * - **Nothing here yet** — no trigger and no steps. The column is the getting
 *   started path rather than an empty form.
 * - **Nothing selected** — the trigger, then the step list.
 * - **A step selected** — the trigger stays (it is the flow's first fact), then
 *   the step's own editor.
 */
import { useMemo } from "react";

import { configSchema, nodeSpec } from "../schema/artifact";
import { plainKind } from "../schema/plain";
import { FieldProvider, type FieldContextValue } from "../inspector/FieldContext";
import { SchemaField } from "../inspector/SchemaField";
import type { ConfigPath } from "../store/paths";
import { useBuilder, useBuilderStore } from "../store/context";
import { StepList } from "./StepList";
import { TriggerSection } from "./TriggerSection";
import { titleOf } from "./title";

export function StepEditor() {
  const store = useBuilderStore();
  const selected = useBuilder((state) => state.selection.nodes);
  const stepCount = useBuilder((state) => state.nodeOrder.length);
  const triggerCount = useBuilder((state) => state.triggers.length);
  const canEdit = useBuilder((state) => state.env.canEdit);

  const triggerSelected = useBuilder((state) => state.triggerSelected);
  const nodeId = selected.length === 1 ? (selected[0] as string) : null;
  const nodeType = useBuilder((state) => (nodeId ? state.nodeType[nodeId] : undefined));
  const config = useBuilder((state) => (nodeId ? state.config[nodeId] : undefined));
  const issues = useBuilder((state) => (nodeId ? state.validation.byNode[nodeId] : undefined));
  const picklists = useBuilder((state) => state.picklists);
  const env = useBuilder((state) => state.env);
  const index = useBuilder((state) => (nodeId ? state.nodeOrder.indexOf(nodeId) : -1));

  const context = useMemo<FieldContextValue | null>(() => {
    if (!nodeId || !nodeType) {
      return null;
    }
    return {
      nodeId,
      nodeType,
      readOnly: !canEdit,
      picklists,
      issues: issues ?? [],
      env,
      set: (path: ConfigPath, value: unknown, historyKey?: string) =>
        value === undefined
          ? store.getState().clearConfig(nodeId, path)
          : store.getState().updateConfig(nodeId, path, value, historyKey),
      clear: (path: ConfigPath) => store.getState().clearConfig(nodeId, path),
    };
  }, [nodeId, nodeType, canEdit, env, picklists, issues, store]);

  if (selected.length > 1) {
    return (
      <aside className="fb-editor" aria-label="Step settings">
        <div className="fb-editor-body">
          <p className="fb-empty">{selected.length} steps selected.</p>
          {canEdit ? (
            <button
              type="button"
              className="btn-pill-secondary btn-pill-sm mt-3"
              onClick={() => store.getState().deleteNodes(selected)}
            >
              Delete these {selected.length} steps
            </button>
          ) : null}
        </div>
      </aside>
    );
  }

  return (
    <aside className="fb-editor" aria-label="Step settings">
      <div className="fb-editor-body">
        <TriggerSection />

        {triggerCount === 0 && stepCount === 0 ? (
          <section className="fb-editor-start">
            <p className="fb-editor-start-title">Two things make a flow</p>
            <p className="fb-editor-start-body">
              Something that starts it, and something it does. Choose what starts it above, then add
              your first step on the canvas.
            </p>
          </section>
        ) : null}

        {stepCount > 0 ? (
          <section className="fb-editor-section">
            <p className="fb-section-label">Steps</p>
            <StepList />
          </section>
        ) : null}

        {nodeId && nodeType && context ? (
          <section className="fb-editor-section">
            <header className="fb-step-head">
              <span className="fb-step-number" aria-hidden="true">
                {index >= 0 ? index + 1 : "·"}
              </span>
              <span className="min-w-0">
                <span className="fb-step-eyebrow block">{plainKind(nodeSpec(nodeType), nodeType)}</span>
                <span className="fb-step-title block">{titleOf(nodeType, config)}</span>
              </span>
            </header>

            <StepProblems issues={issues ?? []} />

            <FieldProvider value={context}>
              <SchemaField
                schema={configSchema(nodeType)}
                path={[]}
                value={config}
                propertyName="config"
                required
              />
            </FieldProvider>

            {canEdit ? (
              <button
                type="button"
                className="fb-step-delete"
                onClick={() => store.getState().deleteNodes([nodeId])}
              >
                Delete this step
              </button>
            ) : null}
          </section>
        ) : triggerSelected ? (
          // The trigger card on the canvas is selected. Its own panel is at the
          // top of this column and is highlighted; saying so beats leaving the
          // column looking like nothing was clicked.
          <p className="fb-empty mt-3">
            What starts this flow is at the top of this column. Open it to change the words it
            watches for, or which account it watches.
          </p>
        ) : stepCount > 0 ? (
          <p className="fb-empty mt-3">Pick a step above, or on the canvas, to change what it says.</p>
        ) : null}
      </div>
    </aside>
  );
}

/**
 * What is wrong with this step, said once, at the top.
 *
 * The old inspector put these behind a "Problems" tab with a count badge, so
 * the reason a step could not go live was one click away from the field that
 * would fix it. The error code is gone from the reader's view — it was
 * `send_message_no_blocks` next to a sentence that already said the same thing
 * in English, and principle 5 of the redesign is that no code reaches the UI.
 */
function StepProblems({ issues }: { issues: readonly { code: string; message: string; severity: string }[] }) {
  const errors = issues.filter((issue) => issue.severity === "error");
  const warnings = issues.filter((issue) => issue.severity !== "error");
  if (errors.length === 0 && warnings.length === 0) {
    return null;
  }
  return (
    <div className="fb-step-problems">
      {[...errors, ...warnings].map((issue, position) => (
        <p key={position} className={`fb-step-problem fb-step-problem-${issue.severity}`}>
          {issue.message}
        </p>
      ))}
    </div>
  );
}
