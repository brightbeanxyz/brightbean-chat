/**
 * The edge's "+" (HANDOFF §3, Flow builder): a step put in the middle of a
 * flow, as one undoable edit.
 */
import { describe, expect, it } from "vitest";

import { sourceHandlesFor } from "../schema/handles";
import { makeDetail } from "../test/fixtures";
import { makeStore } from "../test/render";
import type { FlowGraph } from "../schema/types";

function twoSteps(): FlowGraph {
  return {
    schema: makeDetail().graph.schema,
    nodes: [
      { id: "a", type: "send_message", position: { x: 0, y: 0 }, config: { blocks: [{ type: "text", text: "Hi" }] } },
      { id: "b", type: "send_message", position: { x: 600, y: 0 }, config: { blocks: [{ type: "text", text: "Bye" }] } },
    ],
    edges: [{ id: "e1", source: "a", sourceHandle: "default", target: "b" }],
  };
}

describe("insertBetween", () => {
  it("routes the edge through the new step", () => {
    const store = makeStore(makeDetail(twoSteps()));

    const id = store.getState().insertBetween("e1", "send_message");

    const state = store.getState();
    expect(id).not.toBeNull();
    const edges = state.edgeOrder.map((edgeId) => state.edge[edgeId]);
    expect(edges).toEqual([
      expect.objectContaining({ source: "a", sourceHandle: "default", target: id }),
      expect.objectContaining({ source: id, sourceHandle: sourceHandlesFor("send_message", state.config[id as string])[0], target: "b" }),
    ]);
    expect(state.edge["e1"]).toBeUndefined();
    expect(state.selection.nodes).toEqual([id]);
  });

  it("places the step between the two it joins", () => {
    const store = makeStore(makeDetail(twoSteps()));

    const id = store.getState().insertBetween("e1", "send_message") as string;

    const at = store.getState().position[id];
    expect(at?.x).toBeGreaterThan(0);
    expect(at?.x).toBeLessThan(600);
  });

  it("is one undo step", () => {
    const store = makeStore(makeDetail(twoSteps()));

    store.getState().insertBetween("e1", "send_message");
    store.getState().undo();

    const state = store.getState();
    expect(state.nodeOrder).toEqual(["a", "b"]);
    expect(state.edgeOrder).toEqual(["e1"]);
  });

  it("does nothing for an edge the graph does not have", () => {
    // The trigger card's "starts here" edge is drawn by the canvas, not stored.
    const store = makeStore(makeDetail(twoSteps()));

    expect(store.getState().insertBetween("__trigger__-starts-a", "send_message")).toBeNull();
    expect(store.getState().nodeOrder).toEqual(["a", "b"]);
  });
});
