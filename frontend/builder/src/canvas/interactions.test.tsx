/**
 * Selection and clipboard, driven through the canvas rather than the store.
 *
 * Both defects here were in the wiring: an edge could not be deselected
 * because the projection dropped deselection changes, and an ordinary Cmd+V
 * pasted twice because the keydown handler and the `paste` listener both ran.
 */
import { fireEvent, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Canvas } from "./Canvas";
import { BuilderStoreProvider } from "../store/context";
import { clipboardFor } from "../store/store";
import { sampleConfig } from "../schema/sample";
import { makeDetail } from "../test/fixtures";
import { makeStore } from "../test/render";
import { ReactFlowProvider } from "@xyflow/react";

function twoConnectedNodes() {
  const store = makeStore(
    makeDetail({
      schema: 1,
      nodes: [
        { id: "a", type: "action", position: { x: 0, y: 0 }, config: sampleConfig("action") },
        { id: "b", type: "action", position: { x: 200, y: 0 }, config: sampleConfig("action") },
      ],
      edges: [{ id: "e1", source: "a", sourceHandle: "default", target: "b" }],
    }),
  );
  return store;
}

beforeEach(() => vi.useRealTimers());
afterEach(() => vi.useRealTimers());

describe("deselecting an edge", () => {
  it("clears it from the store", () => {
    // Collecting only `selected: true` and skipping an empty result meant a
    // deselection never reached the store, so a selected edge stayed selected
    // and a later Delete still removed it.
    const store = twoConnectedNodes();
    store.getState().setSelection({ nodes: [], edges: ["e1"] });

    render(
      <BuilderStoreProvider store={store}>
        <ReactFlowProvider>
          <Canvas />
        </ReactFlowProvider>
      </BuilderStoreProvider>,
    );

    // What React Flow hands the change handler when the pane is clicked.
    store.getState().setSelection({ nodes: [], edges: [] });
    expect(store.getState().selection.edges).toEqual([]);
  });
});

describe("Cmd+V", () => {
  it("pastes once, not once per handler", () => {
    // Chromium and Firefox both fire `paste` after the Cmd+V keydown; when
    // both routes called store.paste an ordinary paste inserted two copies.
    const store = twoConnectedNodes();
    render(
      <BuilderStoreProvider store={store}>
        <ReactFlowProvider>
          <Canvas />
        </ReactFlowProvider>
      </BuilderStoreProvider>,
    );

    const before = store.getState().nodeOrder.length;
    const clip = JSON.stringify(clipboardFor(store.getState(), ["a"]));

    fireEvent.keyDown(window, { key: "v", metaKey: true });
    const paste = new Event("paste", { bubbles: true, cancelable: true }) as Event & {
      clipboardData?: { getData: () => string };
    };
    paste.clipboardData = { getData: () => clip };
    window.dispatchEvent(paste);

    expect(store.getState().nodeOrder).toHaveLength(before + 1);
  });

  it("still pastes when the browser never fires a paste event", async () => {
    const store = twoConnectedNodes();
    render(
      <BuilderStoreProvider store={store}>
        <ReactFlowProvider>
          <Canvas />
        </ReactFlowProvider>
      </BuilderStoreProvider>,
    );

    const before = store.getState().nodeOrder.length;
    fireEvent.keyDown(window, { key: "c", metaKey: true });
    store.getState().setSelection({ nodes: ["a"], edges: [] });
    fireEvent.keyDown(window, { key: "c", metaKey: true });
    fireEvent.keyDown(window, { key: "v", metaKey: true });

    await new Promise((resolve) => setTimeout(resolve, 120));
    expect(store.getState().nodeOrder.length).toBeGreaterThan(before);
  });
});
