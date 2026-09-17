/**
 * The trigger cards: what they show, what they dispatch, and what they must
 * never touch.
 *
 * The last group is the important one. These are synthetic ids sitting on the
 * same canvas as real ones, so the property worth pinning is not that they look
 * right — it is that nothing about them can reach the graph being saved.
 *
 * Queried by text rather than by role throughout: React Flow leaves a node
 * hidden until it can measure it, which jsdom never quite does, and getByRole
 * honours that while getByText does not. The edges are asserted on the selector
 * for the same reason — jsdom cannot measure a handle, so an edge that breaks
 * only visually would pass here either way. That gap is covered by looking at
 * the real canvas before merging, not by a test that pretends otherwise.
 */
import { fireEvent, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Canvas } from "./Canvas";
import { selectTriggerEdges, selectTriggerNodes, triggerNodeId } from "./triggerNodes";
import { OPEN_TRIGGER_EVENT, type OpenTriggerDetail } from "./triggerEvents";
import { makeDetail, makeSampleGraph, makeTriggers } from "../test/fixtures";
import { makeStore, renderWith } from "../test/render";
import { toGraph } from "../store/serialize";

const storeWith = (count: number, env = {}) =>
  makeStore(makeDetail(makeSampleGraph(), { triggers: makeTriggers(count) }), env);

function captureOpens(run: () => void): OpenTriggerDetail[] {
  const seen: OpenTriggerDetail[] = [];
  const listen = (event: Event) => seen.push((event as CustomEvent<OpenTriggerDetail>).detail);
  // On window, to prove the event actually bubbles up from body — Alpine's
  // handler in edit.html is a .window one and would never fire otherwise.
  window.addEventListener(OPEN_TRIGGER_EVENT, listen);
  try {
    run();
  } finally {
    window.removeEventListener(OPEN_TRIGGER_EVENT, listen);
  }
  return seen;
}

describe("the trigger cards", () => {
  it("draws one per trigger, with its label and summary", () => {
    renderWith(storeWith(2), <Canvas />);

    expect(screen.getAllByText("Comment")).toHaveLength(2);
    expect(screen.getByText(/WORD1/)).toBeTruthy();
    expect(screen.getByText(/WORD2/)).toBeTruthy();
  });

  it("says what the graph cannot: the public reply lives on the trigger", () => {
    // The whole reason the card exists. A summary reading only "Comments on any
    // post" leaves the reader exactly as confused as the drawer did.
    renderWith(storeWith(1), <Canvas />);

    expect(screen.getByText(/replies publicly/)).toBeTruthy();
  });

  it("offers to add one when the flow has none", () => {
    renderWith(makeStore(makeDetail(makeSampleGraph())), <Canvas />);

    expect(screen.getByText("Add a trigger")).toBeTruthy();
  });

  it("gives a viewer the statement and not the button", () => {
    renderWith(makeStore(makeDetail(makeSampleGraph()), { canEdit: false }), <Canvas />);

    expect(screen.queryByText("Add a trigger")).toBeNull();
    expect(screen.getByText(/this flow will not run when published/)).toBeTruthy();
  });

  it("asks the drawer to open on the trigger that was clicked", () => {
    renderWith(storeWith(2), <Canvas />);

    const seen = captureOpens(() => fireEvent.click(screen.getAllByTitle("Open this trigger")[0]!));

    expect(seen).toHaveLength(1);
    expect(seen[0]?.triggerId).toBe("t1");
  });

  it("asks for a blank form when there is nothing to open", () => {
    renderWith(makeStore(makeDetail(makeSampleGraph())), <Canvas />);

    const seen = captureOpens(() => fireEvent.click(screen.getByText("Add a trigger")));

    expect(seen).toHaveLength(1);
    expect(seen[0]?.triggerId).toBeNull();
  });

  it("marks a disabled trigger as off", () => {
    const store = makeStore(
      makeDetail(makeSampleGraph(), { triggers: makeTriggers(1, { enabled: false }) }),
    );

    renderWith(store, <Canvas />);

    expect(screen.getByText("OFF")).toBeTruthy();
  });
});

describe("the cards cannot reach the graph", () => {
  it("never enters what gets saved", () => {
    const store = makeStore(makeDetail(makeSampleGraph()));
    const before = toGraph(store.getState());

    store.getState().setTriggers(makeTriggers(3));

    expect(toGraph(store.getState())).toEqual(before);
  });

  it("does not make the flow dirty or enter undo history", () => {
    const store = makeStore(makeDetail(makeSampleGraph()));
    const revision = store.getState().revision;
    const history = store.getState().past.length;

    store.getState().setTriggers(makeTriggers(2));

    expect(store.getState().revision).toBe(revision);
    expect(store.getState().past.length).toBe(history);
  });

  it("is never selectable, so a synthetic id cannot reach the inspector", () => {
    const nodes = selectTriggerNodes(storeWith(2).getState());

    expect(nodes).toHaveLength(2);
    expect(nodes.every((node) => node.selectable === false)).toBe(true);
    expect(nodes.every((node) => node.deletable === false)).toBe(true);
    expect(nodes.every((node) => node.draggable === false)).toBe(true);
  });

  it("declares its own size, so the opening view includes it", () => {
    // React Flow bounds a node from the dimensions it has, and it only measures
    // after paint. Without declared ones the cards had no bounds when fitView
    // ran, so the opening view framed the graph and left the trigger stack just
    // off the left edge — the one thing the flow most needs to show.
    const nodes = selectTriggerNodes(storeWith(1).getState());

    expect(nodes[0]?.width).toBeGreaterThan(0);
    expect(nodes[0]?.height).toBeGreaterThan(0);
  });

  it("a delete routed at a synthetic node id is a no-op", () => {
    // The backstop behind selectable:false. Before the store guard, an unknown
    // id still pushed an undo entry and bumped revision, which marked the flow
    // dirty and sent a pointless PUT.
    const store = makeStore(makeDetail(makeSampleGraph()));
    store.getState().setTriggers(makeTriggers(1));
    const revision = store.getState().revision;
    const history = store.getState().past.length;

    store.getState().deleteNodes([triggerNodeId("t1")]);

    expect(store.getState().revision).toBe(revision);
    expect(store.getState().past.length).toBe(history);
  });

  it("a delete routed at a synthetic edge id is a no-op", () => {
    const store = makeStore(makeDetail(makeSampleGraph()));
    store.getState().setTriggers(makeTriggers(1));
    const revision = store.getState().revision;

    store.getState().deleteEdges(["trigger-edge:t1"]);

    expect(store.getState().revision).toBe(revision);
  });
});

describe("the trigger edges", () => {
  it("point at the one node that starts the flow", () => {
    const store = makeStore(
      makeDetail(
        {
          schema: 1,
          nodes: [
            { id: "a", type: "send_message", position: { x: 0, y: 0 }, config: { blocks: [{ type: "text", text: "x" }] } },
            { id: "b", type: "send_message", position: { x: 300, y: 0 }, config: { blocks: [{ type: "text", text: "y" }] } },
          ],
          edges: [{ id: "e1", source: "a", sourceHandle: "default", target: "b" }],
        },
        { triggers: makeTriggers(2) },
      ),
    );

    const edges = selectTriggerEdges(store.getState());

    expect(edges).toHaveLength(2);
    expect(edges.every((edge) => edge.target === "a")).toBe(true);
    expect(edges.every((edge) => edge.deletable === false)).toBe(true);
  });

  it("draws none when entry detection is ambiguous", () => {
    expect(selectTriggerEdges(storeWith(2).getState())).toEqual([]);
  });
});
