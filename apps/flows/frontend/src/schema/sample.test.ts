import { describe, expect, it } from "vitest";

import { validateNode } from "../test/ajv";
import { NODE_TYPES } from "./artifact";
import { HANDLE_PATTERN, ID_PATTERN, sourceHandles } from "./handles";
import { newNodeId } from "./ids";
import { variantChoices } from "./resolve";
import { configSchema } from "./artifact";
import { sampleConfig } from "./sample";

const node = (type: string, config: unknown) => ({ id: newNodeId(), type, position: { x: 0, y: 0 }, config });

describe("a node placed from the palette", () => {
  it.each(NODE_TYPES.map((spec) => spec.type))(
    "%s starts with a config the server would accept",
    (type) => {
      // The first autosave fires two seconds after the drop, so an invalid
      // seed makes a red banner the user's first experience of the builder.
      const outcome = validateNode(node(type, sampleConfig(type)));
      expect(outcome.errors).toEqual([]);
      expect(outcome.valid).toBe(true);
    },
  );

  it.each(NODE_TYPES.map((spec) => spec.type))("%s is also valid with every optional key filled", (type) => {
    expect(validateNode(node(type, sampleConfig(type, { optional: true }))).errors).toEqual([]);
  });
});

describe("every union branch is reachable", () => {
  it.each(NODE_TYPES.map((spec) => spec.type))("%s validates at each variant index", (type) => {
    const tags = variantChoices(configSchema(type));
    const branches = Math.max(tags.length, 4);
    for (let variant = 0; variant < branches; variant += 1) {
      const outcome = validateNode(node(type, sampleConfig(type, { optional: true, variant })));
      expect({ type, variant, errors: outcome.errors }).toEqual({ type, variant, errors: [] });
    }
  });
});

describe("ids the sample generator mints", () => {
  it("match the server's id grammar and produce well-formed handles", () => {
    for (const spec of NODE_TYPES) {
      const config = sampleConfig(spec.type, { optional: true });
      for (const handle of sourceHandles(spec, config)) {
        expect(handle).toMatch(HANDLE_PATTERN);
      }
      expect(newNodeId()).toMatch(ID_PATTERN);
    }
  });

  it("gives every item in one list a distinct id, so handles do not collide", () => {
    const config = sampleConfig("randomizer", { optional: true }) as { paths: { id: string }[] };
    expect(config.paths.length).toBeGreaterThanOrEqual(2);
    expect(new Set(config.paths.map((path) => path.id)).size).toBe(config.paths.length);
  });
});
