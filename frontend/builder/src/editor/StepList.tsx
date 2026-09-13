/**
 * The steps in this flow, as a list you can click through.
 *
 * The canvas is the map; this is the index. Before it, the only way to reach a
 * step's settings was to find its card on the canvas and click it, which is
 * fine for six nodes laid out neatly and hopeless for a graph somebody dragged
 * around — a step could be off-screen with nothing saying it existed.
 */
import { nodeSpec } from "../schema/artifact";
import { plainKind } from "../schema/plain";
import { useBuilder, useBuilderStore } from "../store/context";
import { titleOf } from "./title";

export function StepList() {
  const store = useBuilderStore();
  const order = useBuilder((state) => state.nodeOrder);
  const nodeType = useBuilder((state) => state.nodeType);
  const config = useBuilder((state) => state.config);
  const byNode = useBuilder((state) => state.validation.byNode);
  const selected = useBuilder((state) => state.selection.nodes);

  if (order.length === 0) {
    return null;
  }

  return (
    <ol className="fb-steplist">
      {order.map((id, index) => {
        const type = nodeType[id] ?? "";
        const spec = nodeSpec(type);
        const broken = (byNode[id] ?? []).some((issue) => issue.severity === "error");
        const isSelected = selected.length === 1 && selected[0] === id;
        return (
          <li key={id}>
            <button
              type="button"
              className={`fb-steplist-item${isSelected ? " is-selected" : ""}`}
              onClick={() => store.getState().setSelection({ nodes: [id], edges: [] })}
            >
              <span className="fb-step-number" aria-hidden="true">
                {index + 1}
              </span>
              <span className="min-w-0">
                <span className="fb-step-eyebrow block">{plainKind(spec, type)}</span>
                <span className="fb-steplist-title block truncate">{titleOf(type, config[id])}</span>
              </span>
              {broken ? (
                <span className="fb-step-flag" title="This step needs something before the flow can go live">
                  Needs a fix
                </span>
              ) : null}
            </button>
          </li>
        );
      })}
    </ol>
  );
}
