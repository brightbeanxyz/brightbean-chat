/**
 * Save state, undo/redo, the stats toggle and Publish.
 *
 * Two pieces of copy here are load-bearing. "Saved" and "N problems" are shown
 * side by side rather than folded into one status, because a 200 from the API
 * means the draft *was written* and may still carry graph-stage errors — a
 * draft is allowed to be half-wired. And Publish stays enabled with known
 * errors: what the builder knows is only as of the last save, so disabling it
 * would be a claim it cannot support.
 */
import { useState } from "react";

import { ApiError } from "./api/client";
import type { ValidationPayload } from "./schema/types";
import { publishFlow } from "./api/flows";
import type { Autosave } from "./persistence/autosave";
import { useBuilder, useBuilderStore } from "./store/context";

const SAVE_COPY: Record<string, string> = {
  clean: "No changes",
  dirty: "Unsaved changes",
  saving: "Saving…",
  saved: "Saved",
  rejected: "Not saved",
  error: "Save failed",
};

export function Toolbar({ autosave }: { autosave: Autosave | null }) {
  const store = useBuilderStore();
  const save = useBuilder((state) => state.save);
  const canEdit = useBuilder((state) => state.env.canEdit);
  const statsVisible = useBuilder((state) => state.statsVisible);
  const canUndo = useBuilder((state) => state.past.length > 0);
  const canRedo = useBuilder((state) => state.future.length > 0);
  const errorCount = useBuilder((state) => state.validation.errors.length);
  const warningCount = useBuilder((state) => state.validation.warnings.length);
  const [publishing, setPublishing] = useState(false);

  const publish = async () => {
    setPublishing(true);
    try {
      // Flush first: publishing a draft the server has not seen yet would
      // publish the previous version and look like the button did nothing.
      await autosave?.flush();
      const result = await publishFlow(store.getState().env);
      store.getState().applyValidation(result.validation, store.getState().revision);
      store.getState().setSave({
        state: "saved",
        version: result.version,
        publishedVersion: result.version,
        message: null,
        issues: [],
      });
    } catch (error) {
      if (error instanceof ApiError && error.status === 422) {
        const payload = error.payload as { validation?: ValidationPayload } | null;
        if (payload?.validation) {
          store.getState().applyValidation(payload.validation, store.getState().revision);
        }
        store.getState().setSave({ message: "Publish blocked — fix the errors below." });
      } else if (error instanceof ApiError) {
        store.getState().setSave({ message: error.message });
      }
    } finally {
      setPublishing(false);
    }
  };

  return (
    <div className="fb-toolbar">
      {canEdit ? (
        <>
          <button type="button" className="btn-link text-xs" disabled={!canUndo} onClick={() => store.getState().undo()}>
            Undo
          </button>
          <button type="button" className="btn-link text-xs" disabled={!canRedo} onClick={() => store.getState().redo()}>
            Redo
          </button>
        </>
      ) : null}

      <button
        type="button"
        className="btn-link text-xs"
        aria-pressed={statsVisible}
        onClick={() => store.getState().toggleStats()}
      >
        {statsVisible ? "Hide stats" : "Show stats"}
      </button>

      <span className="ml-auto flex items-center gap-2 text-xs" style={{ color: "var(--text-tertiary)" }}>
        {errorCount > 0 ? <span className="fb-badge fb-badge-error">{errorCount} to fix</span> : null}
        {warningCount > 0 ? <span className="fb-badge fb-badge-warning">{warningCount} to check</span> : null}
        <span data-save-state={save.state}>
          {SAVE_COPY[save.state] ?? save.state}
          {save.version ? ` · Draft v${save.version.version}` : ""}
        </span>
        {canEdit ? (
          <button type="button" className="btn-primary-sm" disabled={publishing} onClick={() => void publish()}>
            {publishing ? "Publishing…" : "Publish"}
          </button>
        ) : null}
      </span>
    </div>
  );
}
