/**
 * Where the trigger stack sits, and when its arrow means anything.
 *
 * Pure, no DOM: jsdom cannot measure a handle, so anything that depends on the
 * edge actually being drawn has to be asserted at this level or not at all.
 */
import { describe, expect, it } from "vitest";

import { triggerAnchor } from "./triggerNodes";
import { makeDetail } from "../test/fixtures";
import { makeStore } from "../test/render";
import { SCHEMA_VERSION } from "../schema/artifact";

const node = (id: string, x: number, y: number) => ({
  id,
  type: "send_message",
  position: { x, y },
  config: { blocks: [{ type: "text", text: "hi" }] },
});

const graph = (nodes: ReturnType<typeof node>[], edges: unknown[] = []) => ({
  schema: SCHEMA_VERSION,
  nodes,
  edges: edges as never[],
});

const anchorFor = (nodes: ReturnType<typeof node>[], edges: unknown[] = []) =>
  triggerAnchor(makeStore(makeDetail(graph(nodes, edges))).getState());

describe("triggerAnchor", () => {
  it("anchors to the single entry node and wires to it", () => {
    const result = anchorFor(
      [node("a", 400, 200), node("b", 700, 200)],
      [{ id: "e1", source: "a", sourceHandle: "default", target: "b" }],
    );

    expect(result.at).toEqual({ x: 400, y: 200 });
    expect(result.wired).toBe("a");
  });

  it("follows the entry node when the author moves it", () => {
    const result = anchorFor(
      [node("a", 40, 900), node("b", 700, 200)],
      [{ id: "e1", source: "a", sourceHandle: "default", target: "b" }],
    );

    expect(result.at).toEqual({ x: 40, y: 900 });
  });

  it("draws no edge when several nodes could start the flow", () => {
    // Already an error the problems rail reports. An arrow into one of them
    // would be asserting something false about which runs first.
    const result = anchorFor([node("a", 400, 200), node("b", 100, 500)]);

    expect(result.wired).toBeNull();
    expect(result.at).toEqual({ x: 100, y: 500 });
  });

  it("draws no edge when nothing can start the flow", () => {
    const result = anchorFor(
      [node("a", 300, 100), node("b", 600, 100)],
      [
        { id: "e1", source: "a", sourceHandle: "default", target: "b" },
        { id: "e2", source: "b", sourceHandle: "default", target: "a" },
      ],
    );

    expect(result.wired).toBeNull();
  });

  it("falls back to the origin on an empty graph", () => {
    expect(anchorFor([])).toEqual({ at: { x: 0, y: 0 }, wired: null });
  });
});
