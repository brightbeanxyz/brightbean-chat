/**
 * "Add a step", floating at the bottom of the canvas.
 *
 * The palette this replaces was a 13rem column of node types whose primary
 * gesture was drag. Three things were wrong with it. It was always on screen
 * whether or not you were adding anything, costing a fifth of the window. Drag
 * is the one gesture that does not survive a trackpad, a touch screen or a
 * keyboard, and clicking dropped the node in the centre of the pane where it
 * usually landed on top of another one. And the types were listed by registry
 * label — "Send Message", "Smart Delay" — so choosing meant already knowing the
 * vocabulary.
 *
 * So: one button, a menu grouped by what the step *does* in plain words, and
 * the new step is placed clear of everything already on the canvas and selected,
 * so the left column is already showing its settings.
 *
 * Drag still works from each menu row, for anyone who preferred it.
 */
import { useReactFlow } from "@xyflow/react";
import { useEffect, useRef, useState } from "react";

import { FALLBACK_GROUP, GROUPS, NODE_TYPES, groupOf } from "../schema/artifact";
import { GROUP_PHRASE } from "../schema/plain";
import { useBuilder, useBuilderStore } from "../store/context";

/** Where a new step goes: to the right of everything, vertically centred on it. */
function nextPosition(positions: readonly { x: number; y: number }[]) {
  if (positions.length === 0) {
    return { x: 0, y: 0 };
  }
  const right = Math.max(...positions.map((position) => position.x));
  const top = Math.min(...positions.map((position) => position.y));
  const bottom = Math.max(...positions.map((position) => position.y));
  return { x: right + 320, y: Math.round((top + bottom) / 2) };
}

export function AddStep() {
  const store = useBuilderStore();
  const canEdit = useBuilder((state) => state.env.canEdit);
  const [open, setOpen] = useState(false);
  const wrapper = useRef<HTMLDivElement>(null);
  const { fitView } = useReactFlow();

  useEffect(() => {
    if (!open) {
      return;
    }
    const onDown = (event: MouseEvent) => {
      if (wrapper.current && !wrapper.current.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    const onEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", onDown);
    window.addEventListener("keydown", onEscape);
    return () => {
      document.removeEventListener("mousedown", onDown);
      window.removeEventListener("keydown", onEscape);
    };
  }, [open]);

  if (!canEdit) {
    return null;
  }

  const add = (type: string) => {
    const state = store.getState();
    const at = nextPosition(state.nodeOrder.map((id) => state.position[id] ?? { x: 0, y: 0 }));
    const id = state.addNode(type, at, { cascade: true });
    setOpen(false);
    if (id) {
      // Selected, so the left column is already editing it, and brought into
      // view — a step placed off-screen reads as a button that did nothing.
      store.getState().setSelection({ nodes: [id], edges: [] });
      window.requestAnimationFrame(() => fitView({ duration: 220, padding: 0.2 }));
    }
  };

  const known = new Set(GROUPS.map((group) => group.key));
  const drawers = GROUPS.map((group) => ({
    ...group,
    // The reader's word for this group, falling back to the registry's own
    // label so a group added in Python is never a blank heading.
    phrase: GROUP_PHRASE[group.key] ?? group.label,
    types: NODE_TYPES.filter((spec) => {
      const key = groupOf(spec);
      return key === group.key || (group.key === FALLBACK_GROUP && !known.has(key));
    }),
  })).filter((drawer) => drawer.types.length > 0);

  return (
    <div className="fb-addstep" ref={wrapper}>
      {open ? (
        <div className="fb-addstep-menu" role="menu" aria-label="Kinds of step">
          {drawers.map((drawer) => (
            <div key={drawer.key}>
              <p className="fb-addstep-group">{drawer.phrase}</p>
              {drawer.types.map((spec) => (
                <button
                  key={spec.type}
                  type="button"
                  role="menuitem"
                  className={`fb-addstep-item fb-node-${drawer.key}`}
                  draggable
                  data-node-type={spec.type}
                  onClick={() => add(spec.type)}
                  onDragStart={(event) => {
                    event.dataTransfer.setData("application/x-brightbean-node", spec.type);
                    event.dataTransfer.effectAllowed = "copy";
                    setOpen(false);
                  }}
                >
                  <span className="fb-addstep-swatch" aria-hidden="true" />
                  <span className="min-w-0">
                    <span className="block truncate">{spec.label}</span>
                    {spec.description ? (
                      <span className="fb-addstep-hint block truncate">{spec.description}</span>
                    ) : null}
                  </span>
                </button>
              ))}
            </div>
          ))}
        </div>
      ) : null}

      <button
        type="button"
        className="fb-addstep-button"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((was) => !was)}
      >
        <span aria-hidden="true">+</span> Add a step
      </button>
    </div>
  );
}
