/**
 * Round-trip fidelity: load → edit → save → reload reproduces the graph.
 *
 * The failure this guards against is specific. Every object in
 * apps/flows/schema/ is closed, so one React Flow field leaking into a PUT is
 * `unknown_node_key` — a 422 that discards the *entire* save, not a key the
 * server ignores. The exact-key-set assertions below are the direct check on
 * that, and they run after real edits rather than only on a freshly loaded
 * graph, because a store that is clean on load can still leak once it has been
 * driven.
 */
import { describe, expect, it } from "vitest";

import { validateGraph } from "../test/ajv";
import { canonical, makeDetail, makeSampleGraph } from "../test/fixtures";
import { makeStore } from "../test/render";
import { fromGraph, sanitizePosition, toGraph } from "./serialize";

const NODE_KEYS = ["config", "id", "position", "type"];
const EDGE_KEYS = ["id", "source", "sourceHandle", "target"];

function assertExactKeys(graph: ReturnType<typeof toGraph>) {
  expect(Object.keys(graph).sort()).toEqual(["edges", "nodes", "schema"]);
  for (const node of graph.nodes) {
    expect(Object.keys(node).sort()).toEqual(NODE_KEYS);
    expect(Object.keys(node.position).sort()).toEqual(["x", "y"]);
  }
  for (const edge of graph.edges) {
    expect(Object.keys(edge).sort()).toEqual(EDGE_KEYS);
  }
}

describe("toGraph / fromGraph", () => {
  it("reproduces a graph covering every node type exactly", () => {
    const graph = makeSampleGraph({ optional: true });

    expect(toGraph(fromGraph(graph))).toEqual(graph);
  });

  it("reproduces it through the store's load path too", () => {
    const graph = makeSampleGraph({ optional: true });
    const store = makeStore(makeDetail(graph));

    expect(toGraph(store.getState())).toEqual(graph);
  });

  it("is stable as a canonical snapshot", () => {
    // Canonicalised, not stringified as-is: graph_json is a Postgres jsonb
    // column, which does not preserve key order, so byte equality was never
    // something a round trip could promise.
    expect(canonical(toGraph(fromGraph(makeSampleGraph())))).toMatchSnapshot();
  });

  it("emits exactly the four keys per node and per edge", () => {
    assertExactKeys(toGraph(fromGraph(makeSampleGraph({ optional: true }))));
  });

  it("produces a graph the schema accepts", () => {
    const outcome = validateGraph(toGraph(fromGraph(makeSampleGraph({ optional: true }))));

    expect(outcome.errors).toEqual([]);
  });
});

describe("after the store has actually been driven", () => {
  it("still emits exactly the four keys, and still round-trips", () => {
    const store = makeStore(makeDetail(makeSampleGraph({ optional: true })));
    const state = () => store.getState();

    // A config edit, a drag, a selection, an added node and a connection —
    // every mutation path that could smuggle a key in.
    const [first, second] = state().nodeOrder as [string, string];
    state().updateConfig(first, ["blocks", 0, "text"], "Edited");
    state().beginDrag();
    state().moveNode(first, { x: 12.3456, y: -8.9 });
    state().endDrag();
    state().setSelection({ nodes: [first, second], edges: [] });
    const added = state().addNode("action", { x: 40, y: 40 });
    state().connect(second, "default", added as string);

    const graph = toGraph(state());
    assertExactKeys(graph);
    expect(validateGraph(graph).errors).toEqual([]);
    // Positions are rounded at the commit point, so the wire value is what the
    // store holds — no separate rounding pass to disagree with.
    expect(graph.nodes.find((node) => node.id === first)?.position).toEqual({ x: 12.35, y: -8.9 });
  });

  it("carries the edit through a reload unchanged", () => {
    const store = makeStore(makeDetail(makeSampleGraph({ optional: true })));
    const first = store.getState().nodeOrder[0] as string;
    store.getState().updateConfig(first, ["blocks", 0, "text"], "Edited");

    const saved = toGraph(store.getState());
    const reloaded = makeStore(makeDetail(saved));

    expect(toGraph(reloaded.getState())).toEqual(saved);
  });

  it("does not normalise a config on load", () => {
    // No applyDefaults(): filling in optional keys on load would make load →
    // save a rewrite, and a reload would then not reproduce what was saved.
    const graph = makeSampleGraph();
    const store = makeStore(makeDetail(graph));

    const randomizer = toGraph(store.getState()).nodes.find((node) => node.type === "randomizer");
    expect(randomizer?.config).not.toHaveProperty("sticky");
  });
});

describe("sanitizePosition", () => {
  it("rounds to two decimals, so a hundred nodes do not spend the byte budget on float noise", () => {
    expect(sanitizePosition({ x: 1.23456, y: -9.87654 })).toEqual({ x: 1.23, y: -9.88 });
  });

  it("turns a non-finite coordinate into zero rather than a document error", () => {
    // React Flow can emit NaN when a pointer event lands before the pane has
    // been measured, and `non_finite_number` discards the whole save.
    expect(sanitizePosition({ x: NaN, y: Infinity })).toEqual({ x: 0, y: 0 });
    expect(sanitizePosition(undefined)).toEqual({ x: 0, y: 0 });
  });
});
