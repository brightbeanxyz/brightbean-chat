/**
 * The generated form, checked against every node type in the artefact.
 *
 * The overrides registry is emptied for most of these on purpose: what is being
 * tested is that the *generic* renderer can configure anything the schema
 * describes, because that is the property a node type added by a later layer
 * depends on. The hand-written widgets are polish on top of it.
 */
import { fireEvent, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { NODE_TYPES } from "../schema/artifact";
import { validateNode } from "../test/ajv";
import { makeDetail, makeSampleGraph } from "../test/fixtures";
import { makeStore, renderWith } from "../test/render";
import { sampleConfig } from "../schema/sample";
import { toGraph } from "../store/serialize";
import { Inspector } from "./Inspector";
import { OVERRIDES } from "./overrides";

let saved: Record<string, unknown>;

beforeEach(() => {
  saved = { ...OVERRIDES };
});

afterEach(() => {
  for (const key of Object.keys(OVERRIDES)) {
    delete OVERRIDES[key];
  }
  Object.assign(OVERRIDES, saved);
});

function withoutOverrides() {
  for (const key of Object.keys(OVERRIDES)) {
    delete OVERRIDES[key];
  }
}

/** A node with only its required config, so optional keys are still absent. */
function openMinimal(type: string) {
  const store = makeStore(
    makeDetail({
      schema: 1,
      nodes: [{ id: "n1", type, position: { x: 0, y: 0 }, config: sampleConfig(type) }],
      edges: [],
    }),
  );
  store.getState().setSelection({ nodes: ["n1"], edges: [] });
  renderWith(store, <Inspector />);
  return { store, id: "n1" };
}

function openNode(type: string) {
  const store = makeStore(makeDetail(makeSampleGraph({ optional: true })));
  const id = store.getState().nodeOrder.find((entry) => store.getState().nodeType[entry] === type) as string;
  store.getState().setSelection({ nodes: [id], edges: [] });
  const view = renderWith(store, <Inspector />);
  return { store, id, view };
}

describe("the generic renderer alone", () => {
  it.each(NODE_TYPES.map((spec) => spec.type))("renders a form for %s without any override", (type) => {
    withoutOverrides();
    const { store, id } = openNode(type);

    // condition's config is a bare $ref and smart_delay's is a bare tagged
    // union — two of eleven with no `config.properties` at all. Both go
    // through the same dispatcher as any nested value, which is why they work.
    expect(screen.getByText(NODE_TYPES.find((spec) => spec.type === type)?.label as string)).toBeInTheDocument();
    expect(validateNode(toGraph(store.getState()).nodes.find((node) => node.id === id)).errors).toEqual([]);
  });

  it("offers all seven message-block kinds, over four underlying shapes", () => {
    // discriminator.mapping has seven keys but oneOf has four branches, because
    // image/audio/video/file all map to block_media. Enumerating branches would
    // silently lose three kinds.
    withoutOverrides();
    openNode("send_message");

    const chooser = screen.getByLabelText("Message blocks") as HTMLSelectElement;
    const values = within(chooser).getAllByRole("option").map((option) => (option as HTMLOptionElement).value);
    for (const kind of ["text", "image", "audio", "video", "file", "card", "gallery"]) {
      expect(values).toContain(kind);
    }
    // Seven tags, four underlying $defs branches.
    expect(values.filter(Boolean)).toHaveLength(7);
  });
});

describe("switching a variant", () => {
  it("replaces the value instead of merging into it", () => {
    // A merge leaves the previous branch's keys behind, every object here is
    // closed, and the result is unknown_config_key — a 422 that discards the
    // whole save rather than just this node.
    withoutOverrides();
    const { store, id } = openNode("send_message");

    fireEvent.change(screen.getByLabelText("Message blocks"), { target: { value: "image" } });

    const block = (store.getState().config[id] as { blocks: Record<string, unknown>[] }).blocks[0];
    expect(block).toHaveProperty("type", "image");
    expect(block).not.toHaveProperty("text");
    expect(validateNode(toGraph(store.getState()).nodes.find((node) => node.id === id)).errors).toEqual([]);
  });
});

describe("clearing an optional field", () => {
  it("removes the key rather than writing an empty string", () => {
    // Most optional strings carry minLength: 1, and every object is closed, so
    // "" is a validation error and null is invalid_config_value. Cleared has
    // to mean absent.
    withoutOverrides();
    const { store, id } = openNode("send_sms");
    expect(store.getState().config[id]).toHaveProperty("media_url");

    fireEvent.click(screen.getByRole("button", { name: /remove media url/i }));

    expect(store.getState().config[id]).not.toHaveProperty("media_url");
    expect(validateNode(toGraph(store.getState()).nodes.find((node) => node.id === id)).errors).toEqual([]);
  });
});

describe("numeric bounds", () => {
  it("come from the schema, not from the widget", () => {
    withoutOverrides();
    openNode("external_request");

    const timeout = screen.getByLabelText(/timeout \(seconds\)/i) as HTMLInputElement;
    // SPEC §11.7 caps it at 10.
    expect(timeout.min).toBe("1");
    expect(timeout.max).toBe("10");
  });
});

describe("the generated form's structure", () => {
  it("does not repeat the same label three times down a nested union", () => {
    // A list of tagged unions used to read: the array's label, then the item's
    // variant label, then the chosen branch's group label — all identical.
    withoutOverrides();
    openNode("send_message");

    expect(screen.getAllByText("Message blocks")).toHaveLength(1);
  });

  it("still gives the variant selector an accessible name", () => {
    withoutOverrides();
    openNode("send_message");

    expect(screen.getByLabelText("Message blocks").tagName).toBe("SELECT");
  });
});

describe("what a newly added item contains", () => {
  it("reads as real placeholder copy, not an ellipsis", () => {
    withoutOverrides();
    const { store, id } = openMinimal("send_message");

    fireEvent.click(screen.getByRole("button", { name: "+ Buttons" }));
    fireEvent.click(screen.getByRole("button", { name: "Add to Buttons" }));

    const buttons = (store.getState().config[id] as { buttons: Record<string, unknown>[] }).buttons;
    expect(buttons[0]?.["label"]).toBe("Button");
    expect(JSON.stringify(buttons)).not.toContain("…");
  });

  it("gives each item a distinct id, so two buttons cannot share a handle", () => {
    withoutOverrides();
    const { store, id } = openMinimal("send_message");

    fireEvent.click(screen.getByRole("button", { name: "+ Buttons" }));
    fireEvent.click(screen.getByRole("button", { name: "Add to Buttons" }));
    fireEvent.click(screen.getByRole("button", { name: "Add to Buttons" }));

    const buttons = (store.getState().config[id] as { buttons: { id: string }[] }).buttons;
    expect(buttons).toHaveLength(2);
    expect(buttons[0]?.id).not.toBe(buttons[1]?.id);
  });

  it("still validates against the schema", () => {
    withoutOverrides();
    const { store, id } = openMinimal("send_message");

    fireEvent.click(screen.getByRole("button", { name: "+ Buttons" }));
    fireEvent.click(screen.getByRole("button", { name: "Add to Buttons" }));

    expect(validateNode(toGraph(store.getState()).nodes.find((node) => node.id === id)).errors).toEqual([]);
  });
});
