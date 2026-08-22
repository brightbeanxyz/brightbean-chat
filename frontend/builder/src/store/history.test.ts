/**
 * Undo/redo, deletion and the clipboard.
 *
 * The clipboard assertions are the interesting ones: pasting has to mint fresh
 * ids for the nodes *and* for the config items that back dynamic handles, then
 * rewrite the pasted edges to match — otherwise a `btn:` handle on a pasted
 * node routes by an id its own config no longer has.
 */
import { describe, expect, it } from "vitest";

import { validateGraph } from "../test/ajv";
import { makeDetail, makeSampleGraph } from "../test/fixtures";
import { makeStore } from "../test/render";
import { ID_PATTERN, sourceHandlesFor } from "../schema/handles";
import { sampleConfig } from "../schema/sample";
import { clipboardFor } from "./store";
import { toGraph } from "./serialize";

function loaded() {
  return makeStore(makeDetail(makeSampleGraph({ optional: true })));
}

/**
 * Three connectable nodes and no edges.
 *
 * The full sample graph deliberately wires every handle of every node, which
 * makes "the edge between a and b" ambiguous — fine for round-trip coverage,
 * useless for asserting what one paste did.
 */
function threeNodes() {
  const store = makeStore(
    makeDetail({
      schema: 1,
      nodes: [
        { id: "a", type: "send_message", position: { x: 0, y: 0 }, config: sampleConfig("send_message", { optional: true }) },
        { id: "b", type: "action", position: { x: 200, y: 0 }, config: sampleConfig("action") },
        { id: "c", type: "action", position: { x: 400, y: 0 }, config: sampleConfig("action") },
      ],
      edges: [],
    }),
  );
  return store;
}

describe("undo and redo", () => {
  it("restores the exact prior graph, and reapplies it", () => {
    const store = loaded();
    const before = toGraph(store.getState());
    const first = store.getState().nodeOrder[0] as string;

    store.getState().updateConfig(first, ["blocks", 0, "text"], "Changed");
    const after = toGraph(store.getState());
    expect(after).not.toEqual(before);

    store.getState().undo();
    expect(toGraph(store.getState())).toEqual(before);

    store.getState().redo();
    expect(toGraph(store.getState())).toEqual(after);
  });

  it("returns a dragged node to where it started, in one step", () => {
    const store = loaded();
    const first = store.getState().nodeOrder[0] as string;
    const origin = store.getState().position[first];

    store.getState().beginDrag();
    for (let frame = 1; frame <= 30; frame += 1) {
      store.getState().moveNode(first, { x: frame * 3, y: frame });
    }
    store.getState().endDrag();

    store.getState().undo();
    expect(store.getState().position[first]).toEqual(origin);
  });

  it("coalesces a burst of typing into one step", () => {
    const store = loaded();
    const first = store.getState().nodeOrder[0] as string;
    const before = toGraph(store.getState());

    for (const text of ["H", "He", "Hel", "Hell", "Hello"]) {
      store.getState().updateConfig(first, ["blocks", 0, "text"], text, "typing");
    }

    store.getState().undo();
    expect(toGraph(store.getState())).toEqual(before);
  });

  it("is an ordinary mutation, so it arms autosave", () => {
    const store = loaded();
    const before = store.getState().revision;
    store.getState().updateConfig(store.getState().nodeOrder[0] as string, ["blocks", 0, "text"], "x");
    store.getState().undo();

    expect(store.getState().revision).toBeGreaterThan(before + 1);
  });

  it("does not record a selection change", () => {
    const store = loaded();
    const depth = store.getState().past.length;

    store.getState().setSelection({ nodes: [store.getState().nodeOrder[0] as string], edges: [] });

    expect(store.getState().past.length).toBe(depth);
    expect(store.getState().revision).toBe(0);
  });
});

describe("deleting", () => {
  it("takes every incident edge with the node", () => {
    const store = loaded();
    const victim = store.getState().nodeOrder[0] as string;
    const incident = store
      .getState()
      .edgeOrder.filter((id) => {
        const edge = store.getState().edge[id];
        return edge?.source === victim || edge?.target === victim;
      });
    expect(incident.length).toBeGreaterThan(0);

    store.getState().deleteNodes([victim]);

    // Leaving them behind would be a pile of dangling_edge on the next save.
    const graph = toGraph(store.getState());
    expect(graph.nodes.some((node) => node.id === victim)).toBe(false);
    expect(graph.edges.some((edge) => edge.source === victim || edge.target === victim)).toBe(false);
    expect(validateGraph(graph).errors).toEqual([]);
  });
});

describe("connecting", () => {
  it("replaces rather than stacks a second edge on one handle", () => {
    // One edge per (source, handle): a second is duplicate_handle_edge.
    const store = threeNodes();

    store.getState().connect("a", "default", "b");
    store.getState().connect("a", "default", "c");

    const fromA = toGraph(store.getState()).edges.filter(
      (edge) => edge.source === "a" && edge.sourceHandle === "default",
    );
    expect(fromA).toHaveLength(1);
    expect(fromA[0]?.target).toBe("c");
  });
});

describe("copy and paste", () => {
  it("mints fresh node ids and keeps only the edges wholly inside the selection", () => {
    const store = threeNodes();
    const [a, b] = ["a", "b"];
    store.getState().connect(a, "default", b);
    const before = store.getState().nodeOrder.length;

    const clip = clipboardFor(store.getState(), [a, b]);
    store.getState().paste(clip);

    const state = store.getState();
    expect(state.nodeOrder).toHaveLength(before + 2);
    const pasted = state.selection.nodes;
    expect(pasted).toHaveLength(2);
    expect(pasted).not.toContain(a);
    for (const id of pasted) {
      expect(id).toMatch(ID_PATTERN);
    }
  });

  it("drops an edge that leaves the selection, rather than pasting a dangling one", () => {
    const store = threeNodes();
    store.getState().connect("a", "default", "b");
    store.getState().connect("b", "default", "c");
    expect(store.getState().edgeOrder).toHaveLength(2);

    store.getState().paste(clipboardFor(store.getState(), ["a", "b"]));

    // a->b comes along; b->c leaves the selection and does not.
    expect(store.getState().edgeOrder).toHaveLength(3);
    expect(validateGraph(toGraph(store.getState())).errors).toEqual([]);
  });

  it("regenerates the item ids that back dynamic handles, and rewrites the edges to match", () => {
    const store = threeNodes();
    const sender = "a";
    const target = "b";
    const buttonHandle = sourceHandlesFor("send_message", store.getState().config[sender]).find((handle) =>
      handle.startsWith("btn:"),
    ) as string;
    expect(buttonHandle).toBeDefined();

    store.getState().connect(sender, buttonHandle, target);

    store.getState().paste(clipboardFor(store.getState(), [sender, target]));

    const state = store.getState();
    const pastedSender = state.selection.nodes.find((id) => state.nodeType[id] === "send_message") as string;
    const pastedHandles = sourceHandlesFor("send_message", state.config[pastedSender]);
    const pastedButton = pastedHandles.find((handle) => handle.startsWith("btn:")) as string;

    // A fresh id…
    expect(pastedButton).not.toBe(buttonHandle);
    // …and the pasted edge follows it, rather than pointing at the old one.
    const pastedEdge = state.edgeOrder
      .map((id) => state.edge[id])
      .find((edge) => edge?.source === pastedSender && edge.sourceHandle.startsWith("btn:"));
    expect(pastedEdge?.sourceHandle).toBe(pastedButton);
    expect(validateGraph(toGraph(state)).errors).toEqual([]);
  });

  it("leaves static handles alone — only the derived ones are remapped", () => {
    const store = threeNodes();
    store.getState().connect("a", "default", "b");

    store.getState().paste(clipboardFor(store.getState(), ["a", "b"]));

    const state = store.getState();
    const pasted = new Set(state.selection.nodes);
    const edge = state.edgeOrder.map((id) => state.edge[id]).find((entry) => entry && pasted.has(entry.source));
    expect(edge?.sourceHandle).toBe("default");
  });

  it("refuses a paste that would exceed max_nodes", () => {
    const store = makeStore(
      makeDetail(makeSampleGraph(), {
        limits: { schema_version: 1, max_graph_bytes: 524288, max_graph_depth: 20, max_nodes: 11, max_edges: 2000 },
      }),
    );
    const before = store.getState().nodeOrder.length;

    store.getState().paste(clipboardFor(store.getState(), store.getState().nodeOrder));

    expect(store.getState().nodeOrder.length).toBe(before);
  });

  it("ignores a payload that is not ours", () => {
    const store = loaded();
    const before = store.getState().nodeOrder.length;

    store.getState().paste({ kind: "something/else", schema: 1, nodes: [], edges: [] } as never);

    expect(store.getState().nodeOrder.length).toBe(before);
  });
});

describe("adding a node where one already is", () => {
  it("cascades rather than stacking, so a second click is visible", () => {
    const store = threeNodes();
    const first = store.getState().addNode("action", { x: 100, y: 100 }) as string;
    const second = store.getState().addNode("action", { x: 100, y: 100 }) as string;

    expect(store.getState().position[first]).toEqual({ x: 100, y: 100 });
    expect(store.getState().position[second]).not.toEqual({ x: 100, y: 100 });
  });

  it("leaves a genuinely free spot alone", () => {
    const store = threeNodes();
    const id = store.getState().addNode("action", { x: -900, y: -900 }) as string;

    expect(store.getState().position[id]).toEqual({ x: -900, y: -900 });
  });

  it("refuses to add past max_nodes", () => {
    const store = makeStore(
      makeDetail(makeSampleGraph(), {
        limits: { schema_version: 1, max_graph_bytes: 524288, max_graph_depth: 20, max_nodes: 11, max_edges: 2000 },
      }),
    );

    expect(store.getState().addNode("action", { x: 0, y: 0 })).toBeNull();
  });
});
