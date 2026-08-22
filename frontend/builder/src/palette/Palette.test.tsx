import { fireEvent, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { GROUPS, NODE_TYPES, groupOf } from "../schema/artifact";
import { validateGraph } from "../test/ajv";
import { makeDetail, makeSampleGraph } from "../test/fixtures";
import { toGraph } from "../store/serialize";
import { makeStore, renderWith } from "../test/render";
import { Palette } from "./Palette";

describe("the palette", () => {
  it("offers every node type in the artefact, exactly once", () => {
    renderWith(makeStore(makeDetail(makeSampleGraph())), <Palette />);

    for (const spec of NODE_TYPES) {
      expect(screen.getAllByRole("button", { name: spec.label })).toHaveLength(1);
    }
  });

  it("renders the drawers in the order and with the labels the artefact gives", () => {
    const { container } = renderWith(makeStore(makeDetail(makeSampleGraph())), <Palette />);

    const headings = [...container.querySelectorAll(".fb-palette-group-label")].map((node) => node.textContent);
    const expected = GROUPS.filter((group) => NODE_TYPES.some((spec) => groupOf(spec) === group.key)).map(
      (group) => group.label,
    );
    expect(headings).toEqual(expected);
  });

  it("files the issue's three groups where the issue says", () => {
    const { container } = renderWith(makeStore(makeDetail(makeSampleGraph())), <Palette />);
    const drawer = (label: string) =>
      [...container.querySelectorAll("div")]
        .filter((node) => node.querySelector(".fb-palette-group-label")?.textContent === label)
        .map((node) => [...within(node).getAllByRole("button")].map((button) => button.textContent))[0];

    expect(drawer("Content")).toEqual(["Send Message", "Data Collection", "Note"]);
    expect(drawer("Logic")).toEqual(["Start Flow", "Condition", "Smart Delay", "Randomizer"]);
    expect(drawer("Actions")).toEqual(["Action", "External Request", "Send SMS", "Send Email"]);
  });

  it("carries the node type on the drag payload the canvas reads", () => {
    renderWith(makeStore(makeDetail(makeSampleGraph())), <Palette />);

    expect(screen.getByRole("button", { name: "Send Message" })).toHaveAttribute("data-node-type", "send_message");
  });

  it("is hidden entirely for a member who cannot edit", () => {
    const { container } = renderWith(
      makeStore(makeDetail(makeSampleGraph()), { canEdit: false }),
      <Palette />,
    );

    expect(container.querySelector(".fb-palette")).toBeNull();
  });
});

describe("adding a node from the palette", () => {
  it("places one on click, so the buttons are not dead ends for a keyboard", () => {
    const store = makeStore(makeDetail({ schema: 1, nodes: [], edges: [] }));
    renderWith(store, <Palette />);

    fireEvent.click(screen.getByRole("button", { name: "Send Message" }));

    expect(store.getState().nodeOrder).toHaveLength(1);
    expect(store.getState().nodeType[store.getState().nodeOrder[0] as string]).toBe("send_message");
  });

  it("seeds it with a config the server accepts, so the first autosave is not a 422", () => {
    const store = makeStore(makeDetail({ schema: 1, nodes: [], edges: [] }));
    renderWith(store, <Palette />);

    fireEvent.click(screen.getByRole("button", { name: "Send Message" }));

    expect(validateGraph(toGraph(store.getState())).errors).toEqual([]);
  });

  it("selects what it just added, so the panel opens on it", () => {
    const store = makeStore(makeDetail({ schema: 1, nodes: [], edges: [] }));
    renderWith(store, <Palette />);

    fireEvent.click(screen.getByRole("button", { name: "Condition" }));

    expect(store.getState().selection.nodes).toEqual(store.getState().nodeOrder);
  });
});
