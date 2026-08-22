/**
 * The flow-level problem list.
 *
 * Deduplicated by code and message, because `multiple_entry_nodes` arrives once
 * per offending node — right for badging each card, wrong for a rail that would
 * otherwise repeat one sentence three times.
 *
 * A code this bundle has never seen still renders. Layer 4 and Layer 5 add
 * codes, and "an error I cannot classify" must never become "no error".
 */
import { useBuilder, useBuilderStore } from "../store/context";
import { railIssues } from "./normalize";

export function ProblemsRail() {
  const store = useBuilderStore();
  const index = useBuilder((state) => state.validation);
  const stale = useBuilder((state) => state.revision > state.validation.revision);
  const message = useBuilder((state) => state.save.message);

  const issues = railIssues(index);

  if (issues.length === 0 && !message) {
    return null;
  }

  return (
    <section className="fb-palette border-t" style={{ borderRight: "none", width: "100%", maxHeight: "12rem" }} aria-label="Problems">
      {message ? <p className="alert-error mb-2">{message}</p> : null}
      {stale && issues.length > 0 ? <p className="fb-empty mb-1">Re-checking…</p> : null}

      {issues.map((issue, index_) => (
        <button
          key={index_}
          type="button"
          className={`fb-problem fb-problem-${issue.severity}`}
          onClick={() =>
            issue.node_id
              ? store.getState().setSelection({ nodes: [issue.node_id], edges: [] })
              : issue.edge_id
                ? store.getState().setSelection({ nodes: [], edges: [issue.edge_id] })
                : undefined
          }
        >
          {issue.message}
          <span className="fb-problem-code block">{issue.code}</span>
        </button>
      ))}
    </section>
  );
}
