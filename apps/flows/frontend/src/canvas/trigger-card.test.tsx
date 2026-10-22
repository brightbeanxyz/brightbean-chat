/**
 * The trigger card is on the canvas and never in the graph.
 *
 * Both halves matter. A flow reads as one sentence — this happens, then this —
 * and the canvas used to start halfway through it: the card badged "Starts
 * here" was a step that answers something, with no sign of what.
 *
 * And the way it is drawn is a standing risk. It is a node to React Flow and
 * not a node to us, so the tests that matter most here are the ones proving no
 * save, no delete and no connection can act on it.
 */
import { fireEvent, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Canvas } from "./Canvas";
import { makeDetail, makeSampleGraph } from "../test/fixtures";
import { makeStore, renderWith } from "../test/render";
import { selectRfEdges, selectRfNodes } from "../store/selectors";
import { toGraph } from "../store/serialize";
import type { TriggerSummary } from "../schema/types";
import { TRIGGER_NODE_ID } from "./TriggerCard";

function trigger(overrides: Partial<TriggerSummary> = {}): TriggerSummary {
  return {
    id: "t1",
    type: "comment",
    type_label: "Comment",
    enabled: true,
    priority: 10,
    summary: "Comments on any post",
    plain: "When someone comments on a post",
    connection: null,
    platforms: ["instagram"],
    ...overrides,
  };
}

const ONE_STEP = {
  schema: 1 as const,
  nodes: [
    {
      id: "n1",
      type: "send_message",
      position: { x: 400, y: 120 },
      config: { blocks: [{ type: "text", text: "Hello" }] },
    },
  ],
  edges: [],
};

describe("it is drawn on the canvas", () => {
  it("says when the flow runs as a sentence, not as a type name and a config line", () => {
    // "Keyword" over "quote, estimate, how much" read as one mashed line.
    const store = makeStore(makeDetail(ONE_STEP, { triggers: [trigger()] }));

    renderWith(store, <Canvas />);

    expect(screen.getByText("When someone comments on a post")).toBeInTheDocument();
  });

  it("wears the same chrome as a step card", () => {
    // Its own width, padding and border said "not a step", which reads as "not
    // part of the flow" — the opposite of why it is on the canvas.
    const store = makeStore(makeDetail(ONE_STEP, { triggers: [trigger()] }));

    const { container } = renderWith(store, <Canvas />);
    const card = container.querySelector(".fb-node-trigger");

    expect(card).not.toBeNull();
    expect(card?.classList.contains("fb-node")).toBe(true);
    expect(card?.querySelector(".fb-node-title")).not.toBeNull();
  });

  it("says so when nothing starts the flow, rather than not being there", () => {
    const store = makeStore(makeDetail(ONE_STEP));

    renderWith(store, <Canvas />);

    expect(screen.getByText(/nothing starts this flow yet/i)).toBeInTheDocument();
  });

  it("sits one column left of the step the flow starts at", () => {
    const store = makeStore(makeDetail(ONE_STEP, { triggers: [trigger()] }));

    const card = selectRfNodes(store.getState()).find((node) => node.id === TRIGGER_NODE_ID);

    expect(card?.position.y).toBe(120);
    expect(card?.position.x).toBeLessThan(400);
  });

  it("joins to that step with one edge", () => {
    const store = makeStore(makeDetail(ONE_STEP, { triggers: [trigger()] }));

    const edges = selectRfEdges(store.getState()).filter((edge) => edge.source === TRIGGER_NODE_ID);

    expect(edges).toHaveLength(1);
    expect(edges[0]?.target).toBe("n1");
  });

  it("draws no edge when there is no single step to start at", () => {
    // Two entry steps is already an error the builder reports; drawing a line
    // to one of them would be picking an answer validation is refusing to pick.
    const store = makeStore(
      makeDetail({
        ...ONE_STEP,
        nodes: [...ONE_STEP.nodes, { ...ONE_STEP.nodes[0]!, id: "n2", position: { x: 400, y: 400 } }],
      }),
    );

    expect(selectRfEdges(store.getState()).filter((edge) => edge.source === TRIGGER_NODE_ID)).toEqual([]);
  });

  it("selects on click, the way a step card does", () => {
    const store = makeStore(makeDetail(ONE_STEP, { triggers: [trigger()] }));
    const { container } = renderWith(store, <Canvas />);

    fireEvent.click(container.querySelector(".fb-node-trigger") as HTMLElement);

    expect(store.getState().triggerSelected).toBe(true);
  });

  it("and selecting it clears any selected step, so only one thing is highlighted", () => {
    const store = makeStore(makeDetail(ONE_STEP, { triggers: [trigger()] }));
    store.getState().setSelection({ nodes: ["n1"], edges: [] });
    const { container } = renderWith(store, <Canvas />);

    fireEvent.click(container.querySelector(".fb-node-trigger") as HTMLElement);

    expect(store.getState().selection.nodes).toEqual([]);
  });

  it("and selecting a step clears it back", () => {
    const store = makeStore(makeDetail(ONE_STEP, { triggers: [trigger()] }));
    store.getState().selectTrigger();

    store.getState().setSelection({ nodes: ["n1"], edges: [] });

    expect(store.getState().triggerSelected).toBe(false);
  });

  it("is never written to the graph by being selected", () => {
    const store = makeStore(makeDetail(ONE_STEP, { triggers: [trigger()] }));
    const before = store.getState().revision;

    store.getState().selectTrigger();

    expect(store.getState().revision).toBe(before);
  });
});

describe("it is not in the graph", () => {
  it("never reaches the saved document", () => {
    // The property everything else rests on. It holds by construction — the
    // card is injected into the projection and never into `nodeOrder` — but a
    // future "sync the projection back" would break it silently.
    const store = makeStore(makeDetail(makeSampleGraph(), { triggers: [trigger()] }));

    const graph = toGraph(store.getState());

    expect(graph.nodes.some((node) => node.id === TRIGGER_NODE_ID)).toBe(false);
    expect(graph.edges.some((edge) => edge.source === TRIGGER_NODE_ID)).toBe(false);
  });

  it("is not draggable, deletable or connectable", () => {
    const store = makeStore(makeDetail(ONE_STEP, { triggers: [trigger()] }));

    const card = selectRfNodes(store.getState()).find((node) => node.id === TRIGGER_NODE_ID);

    expect({
      draggable: card?.draggable,
      deletable: card?.deletable,
      connectable: card?.connectable,
    }).toEqual({ draggable: false, deletable: false, connectable: false });
  });

  it("is selectable, because React Flow gives an unselectable node no pointer events", () => {
    // `hasPointerEvents = isSelectable || isDraggable || <a handler it was
    // passed>`, and this node is neither — so `selectable: false` made the card
    // literally unclickable. Safe: Canvas.tsx drops React Flow's own selection
    // changes for this id, so nothing reaches the store but `selectTrigger()`.
    const store = makeStore(makeDetail(ONE_STEP, { triggers: [trigger()] }));

    const card = selectRfNodes(store.getState()).find((node) => node.id === TRIGGER_NODE_ID);

    expect(card?.selectable).toBe(true);
    expect(store.getState().selection.nodes).toEqual([]);
  });

  it("keeps the same object across reads, so React Flow does not remount it", () => {
    const store = makeStore(makeDetail(ONE_STEP, { triggers: [trigger()] }));

    const first = selectRfNodes(store.getState()).find((node) => node.id === TRIGGER_NODE_ID);
    const second = selectRfNodes(store.getState()).find((node) => node.id === TRIGGER_NODE_ID);

    expect(second).toBe(first);
  });

  it("moves with the step it is pinned to", () => {
    const store = makeStore(makeDetail(ONE_STEP, { triggers: [trigger()] }));

    store.getState().moveNodes([{ id: "n1", position: { x: 900, y: 900 } }]);
    const card = selectRfNodes(store.getState()).find((node) => node.id === TRIGGER_NODE_ID);

    expect(card?.position.y).toBe(900);
  });
});
