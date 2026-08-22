/**
 * The node palette, generated from the artefact.
 *
 * Drawers, their order, their labels and their contents are all data
 * (`x-brightbean.groups` and each node type's `group`), so a node type
 * registered in a later layer appears here the moment `make schema` runs. A
 * type whose group this bundle does not know about lands in the last drawer
 * rather than vanishing — the runtime schema endpoint can be newer than the
 * bundle, and a palette that silently loses a node type is worse than one with
 * an "Other" heading.
 */
import { useReactFlow } from "@xyflow/react";

import { FALLBACK_GROUP, GROUPS, NODE_TYPES, groupOf } from "../schema/artifact";
import { useBuilder, useBuilderStore } from "../store/context";

export function Palette() {
  const canEdit = useBuilder((state) => state.env.canEdit);
  const store = useBuilderStore();
  const { screenToFlowPosition, getViewport } = useReactFlow();

  /**
   * Drag is the primary gesture, but these are <button>s and a button that
   * does nothing when clicked is a dead end — for a mouse, and entirely for a
   * keyboard. Clicking drops the node in the middle of what is on screen.
   */
  const add = (type: string) => {
    const pane = document.querySelector(".react-flow__pane")?.getBoundingClientRect();
    const at = pane
      ? screenToFlowPosition({ x: pane.left + pane.width / 2, y: pane.top + pane.height / 2 })
      : { x: getViewport().x, y: getViewport().y };
    store.getState().addNode(type, at);
  };

  if (!canEdit) {
    return null;
  }

  const known = new Set(GROUPS.map((group) => group.key));
  const drawers = GROUPS.map((group) => ({
    ...group,
    types: NODE_TYPES.filter((spec) => {
      const key = groupOf(spec);
      return key === group.key || (group.key === FALLBACK_GROUP && !known.has(key));
    }),
  })).filter((drawer) => drawer.types.length > 0);

  return (
    <nav className="fb-palette" aria-label="Node palette">
      {drawers.map((drawer) => (
        <div key={drawer.key}>
          <p className="fb-palette-group-label">{drawer.label}</p>
          {drawer.types.map((spec) => (
            <button
              key={spec.type}
              type="button"
              className={`fb-palette-item fb-node-${drawer.key}`}
              draggable
              data-node-type={spec.type}
              title={spec.description}
              onClick={() => add(spec.type)}
              onDragStart={(event) => {
                event.dataTransfer.setData("application/x-brightbean-node", spec.type);
                event.dataTransfer.effectAllowed = "copy";
              }}
            >
              <span className="fb-palette-swatch" aria-hidden="true" />
              <span className="truncate">{spec.label}</span>
            </button>
          ))}
        </div>
      ))}
    </nav>
  );
}
