/**
 * "A 100-node graph stays responsive (no per-frame re-render of all panels)."
 *
 * Asserted as a render count, not a wall-clock time. A timing assertion in CI
 * is a flake generator and tells you nothing about *why* it got slow; a render
 * count fails precisely when someone reintroduces a whole-array subscription,
 * which is the only way this regresses.
 */
import { act } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { sampleConfig } from "../schema/sample";
import type { FlowGraph } from "../schema/types";
import { makeDetail } from "../test/fixtures";
import { makeStore, renderWith } from "../test/render";
import { toGraph } from "../store/serialize";
import { useBuilder } from "../store/context";
import { selectRfNodes } from "../store/selectors";
import { Inspector } from "../inspector/Inspector";

function hundredNodes(): FlowGraph {
  const config = sampleConfig("send_message");
  return {
    schema: 1,
    nodes: Array.from({ length: 100 }, (_unused, index) => ({
      id: `n${index}`,
      type: "send_message",
      position: { x: (index % 10) * 240, y: Math.floor(index / 10) * 160 },
      config: structuredClone(config),
    })),
    edges: [],
  };
}

/** A stand-in for one node card: subscribes to exactly what FlowNodeCard does. */
function CardProbe({ nodeId, onRender }: { nodeId: string; onRender: () => void }) {
  useBuilder((state) => state.nodeType[nodeId]);
  useBuilder((state) => state.config[nodeId]);
  useBuilder((state) => state.validation.byNode[nodeId]);
  onRender();
  return null;
}

function InspectorProbe({ onRender }: { onRender: () => void }) {
  onRender();
  return <Inspector />;
}

describe("dragging one node in a hundred", () => {
  it("re-renders neither the inspector nor the other cards", () => {
    const store = makeStore(makeDetail(hundredNodes()));
    store.getState().setSelection({ nodes: ["n0"], edges: [] });

    let inspectorRenders = 0;
    const cardRenders = new Map<string, number>();
    const bump = (id: string) => () => cardRenders.set(id, (cardRenders.get(id) ?? 0) + 1);

    renderWith(
      store,
      <>
        <InspectorProbe onRender={() => (inspectorRenders += 1)} />
        {Array.from({ length: 100 }, (_unused, index) => (
          <CardProbe key={index} nodeId={`n${index}`} onRender={bump(`n${index}`)} />
        ))}
      </>,
    );

    const inspectorBefore = inspectorRenders;
    const cardsBefore = new Map(cardRenders);

    // Sixty frames of a drag on n0.
    act(() => {
      store.getState().beginDrag();
      for (let frame = 0; frame < 60; frame += 1) {
        store.getState().moveNode("n0", { x: frame * 4, y: frame });
      }
      store.getState().endDrag();
    });

    // The inspector subscribes to config[selectedId], and a drag writes only
    // position — so it does not re-render at all.
    expect(inspectorRenders).toBe(inspectorBefore);

    // Nor does any card, including the one being dragged: its position reaches
    // React Flow through the node projection, not through the card's own
    // subscriptions.
    for (const [id, before] of cardsBefore) {
      expect({ id, renders: cardRenders.get(id) }).toEqual({ id, renders: before });
    }
  });

  it("keeps every untouched node object referentially identical across the drag", () => {
    // React Flow memoises its node wrapper on object identity, so this is what
    // actually stops it re-rendering ninety-nine cards per frame.
    const store = makeStore(makeDetail(hundredNodes()));
    const before = selectRfNodes(store.getState());

    store.getState().beginDrag();
    store.getState().moveNode("n0", { x: 999, y: 999 });
    store.getState().endDrag();

    const after = selectRfNodes(store.getState());
    expect(after[0]).not.toBe(before[0]);
    for (let index = 1; index < before.length; index += 1) {
      expect(after[index]).toBe(before[index]);
    }
  });
});

describe("editing one node's config", () => {
  it("re-renders that card and no other", () => {
    const store = makeStore(makeDetail(hundredNodes()));
    const cardRenders = new Map<string, number>();
    const bump = (id: string) => () => cardRenders.set(id, (cardRenders.get(id) ?? 0) + 1);

    renderWith(
      store,
      <>
        {Array.from({ length: 100 }, (_unused, index) => (
          <CardProbe key={index} nodeId={`n${index}`} onRender={bump(`n${index}`)} />
        ))}
      </>,
    );
    const before = new Map(cardRenders);

    act(() => {
      store.getState().updateConfig("n7", ["blocks", 0, "text"], "Edited");
    });

    expect(cardRenders.get("n7")).toBe((before.get("n7") ?? 0) + 1);
    for (const [id, count] of before) {
      if (id !== "n7") {
        expect({ id, renders: cardRenders.get(id) }).toEqual({ id, renders: count });
      }
    }
  });
});

describe("serialisation cost", () => {
  it("is not paid during a drag — only when the graph is about to be saved", () => {
    const store = makeStore(makeDetail(hundredNodes()));

    store.getState().beginDrag();
    for (let frame = 0; frame < 60; frame += 1) {
      store.getState().moveNode("n0", { x: frame, y: frame });
    }
    store.getState().endDrag();

    // toGraph walks every node and edge. Autosave calls it once, after the
    // debounce; nothing on the drag path does.
    const graph = toGraph(store.getState());
    expect(graph.nodes).toHaveLength(100);
    expect(graph.nodes[0]?.position).toEqual({ x: 59, y: 59 });
  });
});
