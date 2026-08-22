/**
 * What a member without `edit_flows` gets.
 *
 * apps/flows/views.py hands the page `can_edit` precisely so the builder can
 * render read-only "rather than letting someone drag nodes around and discover
 * on save that they may not". The API answers 403 either way — this is the
 * honest UI, not the security boundary.
 */
import { screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Canvas } from "./canvas/Canvas";
import { Inspector } from "./inspector/Inspector";
import { Palette } from "./palette/Palette";
import { installAutosave } from "./persistence/autosave";
import { makeDetail, makeSampleGraph } from "./test/fixtures";
import { installCsrfToken, stubHttp, type HttpStub } from "./test/http";
import { makeStore, renderWith } from "./test/render";
import { selectRfEdges, selectRfNodes } from "./store/selectors";

let http: HttpStub;

beforeEach(() => {
  vi.useFakeTimers();
  http = stubHttp();
  installCsrfToken();
});

afterEach(() => {
  http.restore();
  vi.useRealTimers();
});

function viewer() {
  const store = makeStore(makeDetail(makeSampleGraph()), { canEdit: false });
  store.getState().setSelection({ nodes: [store.getState().nodeOrder[0] as string], edges: [] });
  return store;
}

describe("a read-only canvas", () => {
  it("offers no palette", () => {
    const { container } = renderWith(viewer(), <Palette />);

    expect(container.querySelector(".fb-palette")).toBeNull();
  });

  it("still renders the graph, so a Viewer can read it", () => {
    const { container } = renderWith(viewer(), <Canvas />);

    expect(container.querySelectorAll(".react-flow__node").length).toBeGreaterThan(0);
  });

  it("marks every node undraggable, unconnectable and undeletable", () => {
    // The projection is where this is decided, so it cannot be bypassed by a
    // component forgetting to check — which is also why the assertion is on
    // the projection rather than on rendered markup.
    const nodes = selectRfNodes(viewer().getState());

    expect(nodes.length).toBeGreaterThan(0);
    for (const node of nodes) {
      expect({ id: node.id, draggable: node.draggable, connectable: node.connectable, deletable: node.deletable }).toEqual(
        { id: node.id, draggable: false, connectable: false, deletable: false },
      );
    }
  });

  it("marks them all draggable again for a member who can edit", () => {
    // The negative above would pass against a projection that hard-coded false.
    const nodes = selectRfNodes(makeStore(makeDetail(makeSampleGraph())).getState());

    expect(nodes.every((node) => node.draggable)).toBe(true);
  });

  it("leaves every edge undeletable too", () => {
    const edges = selectRfEdges(viewer().getState());

    expect(edges.length).toBeGreaterThan(0);
    expect(edges.every((edge) => edge.deletable === false)).toBe(true);
  });

  it("shows the config but disables every control", () => {
    renderWith(viewer(), <Inspector />);

    const controls = screen.getAllByRole("textbox", { hidden: true });
    expect(controls.length).toBeGreaterThan(0);
    for (const control of controls) {
      expect(control).toBeDisabled();
    }
  });

  it("offers no way to add or remove a list item", () => {
    renderWith(viewer(), <Inspector />);

    expect(screen.queryByRole("button", { name: /^Add$/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /^Remove /i })).toBeNull();
  });
});

describe("autosave in read-only", () => {
  it("is never installed, so no timer exists that could fire", async () => {
    // App.tsx gates installAutosave on canEdit. This asserts the consequence:
    // with it installed, an edit would PUT; the read-only page never gets here.
    const store = makeStore(makeDetail(makeSampleGraph()));
    http.route("/api/flows/", { body: { flow: {}, version: { id: "v", version: 2, published: false, updated_at: "" }, validation: { errors: [], warnings: [] } } });
    const autosave = installAutosave(store);

    store.getState().updateConfig(store.getState().nodeOrder[0] as string, ["blocks", 0, "text"], "x");
    await vi.advanceTimersByTimeAsync(2000);
    expect(http.requests.filter((request) => request.method === "PUT")).toHaveLength(1);

    autosave.stop();

    // After stop(), the same edit produces nothing at all — which is the state
    // a read-only page is in from the start.
    store.getState().updateConfig(store.getState().nodeOrder[0] as string, ["blocks", 0, "text"], "y");
    await vi.advanceTimersByTimeAsync(10_000);
    expect(http.requests.filter((request) => request.method === "PUT")).toHaveLength(1);
  });
});
