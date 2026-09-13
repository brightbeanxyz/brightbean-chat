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
  it("shows what starts the flow, beside the step that starts it", () => {
    const store = makeStore(makeDetail(ONE_STEP, { triggers: [trigger()] }));

    renderWith(store, <Canvas />);

    expect(screen.getByText("Comment")).toBeInTheDocument();
    expect(screen.getByText("Comments on any post")).toBeInTheDocument();
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

  it("opens the drawer that edits triggers rather than editing them itself", () => {
    const store = makeStore(makeDetail(ONE_STEP, { triggers: [trigger()] }));
    renderWith(store, <Canvas />);
    let opened = 0;
    window.addEventListener("toggle-triggers", () => (opened += 1));

    fireEvent.click(screen.getByText("Comment").closest(".fb-trigger-card") as HTMLElement);

    expect(opened).toBe(1);
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

  it("is not draggable, deletable, connectable or selectable", () => {
    const store = makeStore(makeDetail(ONE_STEP, { triggers: [trigger()] }));

    const card = selectRfNodes(store.getState()).find((node) => node.id === TRIGGER_NODE_ID);

    expect({
      draggable: card?.draggable,
      deletable: card?.deletable,
      connectable: card?.connectable,
      selectable: card?.selectable,
    }).toEqual({ draggable: false, deletable: false, connectable: false, selectable: false });
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
