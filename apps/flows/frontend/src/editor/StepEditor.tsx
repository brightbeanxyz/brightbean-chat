/**
 * The inspector: the selected step, and nothing else (HANDOFF §3, Flow builder).
 *
 * Two tabs over one step — Settings, the form, and Preview, the step as the
 * person on the other end sees it. The preview used to sit above the form in a
 * stack with the step list, so the field being edited was pushed half a screen
 * down by two things that were not it. The step list moved to the outline on
 * the left; the preview is a tab away, on the same step.
 */
import { useEffect, useMemo, useRef, useState } from "react";

import { configSchema } from "../schema/artifact";
import { FieldProvider, type FieldContextValue } from "../inspector/FieldContext";
import { SchemaField } from "../inspector/SchemaField";
import type { ConfigPath } from "../store/paths";
import { Preview } from "../preview/Preview";
import { useBuilder, useBuilderStore } from "../store/context";
import { titleOf } from "./title";

export function StepEditor() {
  const store = useBuilderStore();
  const bodyRef = useRef<HTMLDivElement>(null);
  const editHeadRef = useRef<HTMLElement>(null);
  const selected = useBuilder((state) => state.selection.nodes);
  const stepCount = useBuilder((state) => state.nodeOrder.length);
  const canEdit = useBuilder((state) => state.env.canEdit);

  const triggerSelected = useBuilder((state) => state.triggerSelected);
  const nodeId = selected.length === 1 ? (selected[0] as string) : null;
  const nodeType = useBuilder((state) => (nodeId ? state.nodeType[nodeId] : undefined));
  const config = useBuilder((state) => (nodeId ? state.config[nodeId] : undefined));
  const issues = useBuilder((state) => (nodeId ? state.validation.byNode[nodeId] : undefined));
  const picklists = useBuilder((state) => state.picklists);
  const env = useBuilder((state) => state.env);
  const index = useBuilder((state) => (nodeId ? state.nodeOrder.indexOf(nodeId) : -1));
  const [tab, setTab] = useState<"settings" | "preview">("settings");

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

  useEffect(() => {
    const body = bodyRef.current;
    const head = editHeadRef.current;
    if (nodeId && body && head) {
      body.scrollTop += head.getBoundingClientRect().top - body.getBoundingClientRect().top;
    }
  }, [nodeId]);

  if (selected.length > 1) {
    return (
      <section className="fb-editor" aria-label="Step settings">
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
      </section>
    );
  }

  return (
    <section className="fb-editor" aria-label="Step settings">
      {nodeId && nodeType && context ? (
        <div className="tabs tabs-inline fb-inspector-tabs" role="tablist" aria-label="Step">
          <button type="button" role="tab" className="tab" aria-selected={tab === "settings"} onClick={() => setTab("settings")}>
            Settings
          </button>
          <button type="button" role="tab" className="tab" aria-selected={tab === "preview"} onClick={() => setTab("preview")}>
            Preview
          </button>
        </div>
      ) : null}
      <div className="fb-editor-body" ref={bodyRef}>
        {nodeId && nodeType && context && tab === "preview" ? (
          <Preview />
        ) : nodeId && nodeType && context ? (
          <section className="fb-editor-section">
            {/* Captioned, ruled off, and pinned.
                
                This header used to repeat the selected row's own number,
                eyebrow and title, in the same components, eight pixels beneath
                it — so the editor read as a seventh row that started counting
                again at one. "Editing step 4" is the whole fix: the number
                stops being a duplicate the moment something says it is the same
                step. The numbered circle and the eyebrow go with it, because the
                caption now carries the identity and the row above still carries
                the kind.

                Sticky because the list answers "what am I editing?" only while
                the list is on screen, and a send_message form scrolls well past
                it. The title wraps rather than truncating: it is the thing
                being worked on, and it was being cut twice at two widths. */}
            <header className="fb-edit-head" ref={editHeadRef}>
              <span className="fb-edit-caption">
                {index >= 0 ? `Editing step ${index + 1}` : "Editing this step"}
              </span>
              <span className="fb-edit-title">{titleOf(nodeType, config)}</span>
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

            {/* Below a rule, on its own. It used to sit in the same cluster as
                the controls that *add* things, in the same register, so the one
                irreversible action on the panel was a few pixels from "add a
                button" and looked no different. */}
            {canEdit ? (
              <>
                <hr className="fb-step-rule" />
                <button
                  type="button"
                  className="fb-step-delete"
                  onClick={() => store.getState().deleteNodes([nodeId])}
                >
                  Delete this step
                </button>
              </>
            ) : null}
          </section>
        ) : triggerSelected ? (
          // The trigger card on the canvas is selected. Point to the controls
          // in the left sidebar so the selection has a clear next step.
          <p className="fb-empty mt-3">What starts this flow is in “When it runs” on the left.</p>
        ) : stepCount > 0 ? (
          <p className="fb-empty mt-3">Pick a step in the outline, or on the canvas, to change what it says.</p>
        ) : (
          <p className="fb-empty mt-3">Add a step on the canvas, then its settings appear here.</p>
        )}
      </div>
    </section>
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
