/**
 * The property the whole design exists for.
 *
 * L4-E adds `external_request`'s runtime; L5-D/E add SMS and email; later
 * layers add node types nobody has written yet. The brief's requirement is that
 * "adding a node type later must require no bespoke canvas code beyond an
 * optional custom panel" — so this injects a twelfth node type into the schema
 * artefact and asserts the whole builder handles it: palette, canvas card,
 * derived handles, a usable form, and a config the server would accept.
 *
 * Nothing in src/ is changed for it. If this test ever needs a source edit to
 * pass, the data-driven claim has stopped being true.
 */
import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("@flow-schema", async () => {
  const actual = (await vi.importActual<{ default: Record<string, never> }>("@flow-schema")).default;
  const document = structuredClone(actual) as unknown as {
    $defs: Record<string, unknown>;
    "x-brightbean": { node_types: unknown[]; groups: { key: string; label: string }[] };
  };

  document.$defs["escalation_step"] = {
    type: "object",
    additionalProperties: false,
    required: ["id", "after_minutes"],
    properties: {
      id: { type: "string", pattern: "^[A-Za-z0-9_-]{1,64}$", minLength: 1, maxLength: 64 },
      after_minutes: { type: "integer", minimum: 1, maximum: 1440 },
      note: { type: "string", maxLength: 200 },
    },
  };
  document.$defs["node_escalate"] = {
    type: "object",
    additionalProperties: false,
    description: "Invented by a later layer. Nothing in the bundle knows it exists.",
    required: ["id", "type", "position", "config"],
    properties: {
      id: { type: "string", pattern: "^[A-Za-z0-9_-]{1,64}$", minLength: 1, maxLength: 64 },
      type: { type: "string", const: "escalate" },
      position: { $ref: "#/$defs/position" },
      config: {
        type: "object",
        additionalProperties: false,
        required: ["policy", "steps"],
        properties: {
          policy: { type: "string", enum: ["round_robin", "first_free"] },
          steps: { type: "array", minItems: 1, maxItems: 5, items: { $ref: "#/$defs/escalation_step" } },
          window: { $ref: "#/$defs/continue_window" },
          reminder: { $ref: "#/$defs/message_block" },
        },
      },
    },
  };

  const node = document.$defs["node"] as {
    oneOf: unknown[];
    discriminator: { mapping: Record<string, string> };
  };
  node.oneOf.push({ $ref: "#/$defs/node_escalate" });
  node.discriminator.mapping["escalate"] = "#/$defs/node_escalate";

  document["x-brightbean"].node_types.push({
    type: "escalate",
    label: "Escalate",
    description: "Invented by a later layer.",
    // Deliberately a group this bundle has never heard of.
    group: "operations",
    handles: ["default", "error"],
    dynamic_handles: [{ prefix: "btn", config_key: "steps" }],
    terminal: false,
    annotation: false,
  });

  return { default: document };
});

const { NODE_TYPES, configSchema, nodeSpec } = await import("./artifact");
const { sourceHandles } = await import("./handles");
const { sampleConfig } = await import("./sample");
const { validateNode } = await import("../test/ajv");
const { Palette } = await import("../palette/Palette");
const { Inspector } = await import("../inspector/Inspector");
const { Canvas } = await import("../canvas/Canvas");
const { makeDetail } = await import("../test/fixtures");
const { makeStore, renderWith } = await import("../test/render");

const spec = () => nodeSpec("escalate") as NonNullable<ReturnType<typeof nodeSpec>>;

function graphWithEscalate() {
  return makeDetail({
    schema: 1,
    nodes: [
      {
        id: "x1",
        type: "escalate",
        position: { x: 0, y: 0 },
        config: sampleConfig("escalate", { optional: true }),
      },
    ],
    edges: [],
  });
}

describe("a node type this bundle has never seen", () => {
  it("is in the registry, read straight from the artefact", () => {
    expect(NODE_TYPES.map((entry) => entry.type)).toContain("escalate");
  });

  it("gets a placeable config the server would accept", () => {
    const config = sampleConfig("escalate");
    const outcome = validateNode({ id: "x1", type: "escalate", position: { x: 0, y: 0 }, config });

    expect(outcome.errors).toEqual([]);
  });

  it("derives its dynamic handles from its own config, with no prefix named in source", () => {
    const config = sampleConfig("escalate", { optional: true }) as { steps: { id: string }[] };

    expect(sourceHandles(spec(), config)).toEqual([
      "default",
      "error",
      ...config.steps.map((step) => `btn:${step.id}`),
    ]);
  });

  it("appears in the palette, under the fallback drawer rather than vanishing", () => {
    // Its group is one no drawer declares. A palette that silently dropped it
    // would be worse than one with an "Other" heading.
    const store = makeStore(graphWithEscalate());

    renderWith(store, <Palette />);

    const item = screen.getByRole("button", { name: /escalate/i });
    expect(item).toBeInTheDocument();
    expect(item.closest("div")?.querySelector("p")?.textContent).toBe("Other");
  });

  it("draws a card on the canvas", () => {
    const store = makeStore(graphWithEscalate());

    const { container } = renderWith(store, <Canvas />);

    expect(container.querySelector('[data-node-type="escalate"]')).not.toBeNull();
  });

  it("gets a working form covering enums, arrays, nested objects and unions", () => {
    const store = makeStore(graphWithEscalate());
    store.getState().setSelection({ nodes: ["x1"], edges: [] });

    renderWith(store, <Inspector />);

    expect(screen.getByLabelText("Policy")).toBeInTheDocument();
    expect(screen.getByLabelText(/after minutes/i)).toBeInTheDocument();
    expect(screen.getByText("Steps")).toBeInTheDocument();
    // `humanize()` is the fallback when the copy table has no entry.
    expect(screen.getByLabelText("Note")).toBeInTheDocument();
  });

  it("has a description on its config schema, which the panel can show", () => {
    expect(configSchema("escalate")).toBeDefined();
    expect(spec().description).toContain("later layer");
  });
});
