/**
 * Proof that the canvas actually renders under jsdom.
 *
 * React Flow needs ResizeObserver, DOMMatrixReadOnly and a non-zero bounding
 * box before it will draw a single node. Without them it mounts at zero size
 * and renders nothing — and every "is this node type placeable" assertion in
 * the rest of the suite would then pass against an empty container. This test
 * exists so that failure mode is loud instead of invisible.
 */
import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Canvas } from "../canvas/Canvas";
import { NODE_TYPES } from "../schema/artifact";
import { makeSampleGraph } from "./fixtures";
import { makeDetail } from "./fixtures";
import { makeStore, renderWith } from "./render";

describe("the jsdom environment", () => {
  it("renders React Flow nodes, so canvas assertions are not vacuous", () => {
    const store = makeStore(makeDetail(makeSampleGraph()));

    const { container } = renderWith(store, <Canvas />);

    expect(container.querySelectorAll(".react-flow__node").length).toBe(NODE_TYPES.length);
  });

  it("draws every node type from the artefact, by label", () => {
    const store = makeStore(makeDetail(makeSampleGraph()));

    renderWith(store, <Canvas />);

    for (const spec of NODE_TYPES) {
      expect(screen.getAllByText(spec.label).length).toBeGreaterThan(0);
    }
  });
});
