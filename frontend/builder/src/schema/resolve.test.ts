import { describe, expect, it } from "vitest";

import { operatorCopy } from "../canvas/previews";
import { SCHEMA, configSchema } from "./artifact";
import {
  anyOfRequirements,
  branchAt,
  branchLabels,
  constProperties,
  deref,
  isTaggedUnion,
  isUntaggedUnion,
  matchBranch,
  typesOf,
  variantChoices,
  variantFor,
  variantSchema,
} from "./resolve";

describe("deref", () => {
  it("follows a local $defs reference", () => {
    expect(deref({ $ref: "#/$defs/position" })?.properties).toHaveProperty("x");
  });

  it("refuses a reference this module has not been taught about", () => {
    // Silently rendering an empty form for an unresolvable ref would produce a
    // config the server rejects, with nothing to say why.
    expect(() => deref({ $ref: "https://example.com/other.json#/x" })).toThrow(/only local/);
    expect(() => deref({ $ref: "#/$defs/does_not_exist" })).toThrow(/does not exist/);
  });
});

describe("tagged unions", () => {
  it("reads its choices from the discriminator mapping, not the oneOf branches", () => {
    const block = deref(SCHEMA.$defs["message_block"]);
    expect(isTaggedUnion(block)).toBe(true);
    // Seven tags…
    expect(variantChoices(block)).toHaveLength(7);
    // …over four shapes, because image/audio/video/file all map to block_media.
    expect(block?.oneOf).toHaveLength(4);
    expect(variantSchema(block, "image")).toBe(variantSchema(block, "video"));
  });

  it("picks the branch matching a value's current tag", () => {
    const block = deref(SCHEMA.$defs["message_block"]);
    expect(variantFor(block, { type: "text", text: "hi" })?.properties).toHaveProperty("text");
    expect(variantFor(block, { type: "unheard_of" })).toBeUndefined();
    expect(variantFor(block, null)).toBeUndefined();
  });

  it("recognises the two node configs that are not plain objects", () => {
    // condition's config is a bare $ref and smart_delay's a bare tagged union,
    // so a renderer reaching for `config.properties` breaks on both.
    expect(deref(configSchema("condition"))?.properties).toHaveProperty("rules");
    expect(isTaggedUnion(deref(configSchema("smart_delay")))).toBe(true);
    expect(configSchema("condition")?.properties).toBeUndefined();
  });
});

describe("anyOfRequirements", () => {
  it("reads the media block's either/or", () => {
    expect(anyOfRequirements(deref(SCHEMA.$defs["block_media"]))).toEqual([["media_id"], ["url"]]);
  });

  it("returns nothing for a shape it does not recognise", () => {
    expect(anyOfRequirements({ anyOf: [{ type: "string" }, { type: "number" }] })).toEqual([]);
    expect(anyOfRequirements({ type: "object" })).toEqual([]);
  });
});

describe("typesOf", () => {
  it("normalises the string and array forms the artefact both use", () => {
    expect(typesOf({ type: "string" })).toEqual(["string"]);
    expect(typesOf({ type: ["string", "number", "boolean", "null"] })).toHaveLength(4);
    expect(typesOf({})).toEqual([]);
  });
});

describe("untagged unions", () => {
  it("recognises contract 8's condition rules, which have no discriminator", () => {
    const rules = deref(deref(SCHEMA.$defs["condition_filter"])?.properties?.["rules"]);
    const item = deref(rules?.items);

    expect(isUntaggedUnion(item)).toBe(true);
    expect(isTaggedUnion(item)).toBe(false);
    // Eight branches over six sources: two sources contribute a presence form
    // and a comparison form, so `source` alone could not be a discriminator.
    expect(item?.oneOf).toHaveLength(8);
    expect(branchLabels(item)).toContain("Tag");
    expect(branchLabels(item)).toContain("Custom field comparison");
  });

  it("narrows the operators per source, so the bundle needs no operator table", () => {
    const item = deref(deref(deref(SCHEMA.$defs["condition_filter"])?.properties?.["rules"])?.items);
    const tag = branchAt(item, branchLabels(item).indexOf("Tag"));

    expect(deref(tag?.properties?.["op"])?.enum).toEqual(["has", "has_not"]);
    expect(constProperties(tag)).toEqual({ source: "tag" });
  });

  it("matches a value to its branch on pinned literals, required keys and closedness", () => {
    const item = deref(deref(deref(SCHEMA.$defs["condition_filter"])?.properties?.["rules"])?.items);
    const labels = branchLabels(item);
    const key = "00000000-0000-0000-0000-000000000000";

    // Presence and comparison differ only by `value`; every object is closed,
    // so the extra key is what tells them apart.
    const presence = matchBranch(item, { source: "custom_field", key, op: "has_value" });
    const comparison = matchBranch(item, { source: "custom_field", key, op: "is", value: "x" });

    expect(labels[presence]).toBe("Custom field");
    expect(labels[comparison]).toBe("Custom field comparison");
    expect(presence).not.toBe(comparison);
  });

  it("returns -1 for a value that belongs to no branch", () => {
    const item = deref(deref(deref(SCHEMA.$defs["condition_filter"])?.properties?.["rules"])?.items);

    expect(matchBranch(item, { source: "invented_later", op: "is" })).toBe(-1);
    expect(matchBranch(item, null)).toBe(-1);
  });
});

describe("operator copy", () => {
  it("covers every operator the schema allows, without listing them", () => {
    // The words are derived, so an operator apps/contacts/conditions.py adds
    // later renders sensibly with no edit here.
    const item = deref(deref(deref(SCHEMA.$defs["condition_filter"])?.properties?.["rules"])?.items);
    const ops = new Set<string>();
    for (const branch of item?.oneOf ?? []) {
      for (const op of deref(deref(branch)?.properties?.["op"])?.enum ?? []) {
        ops.add(String(op));
      }
    }

    expect(ops.size).toBeGreaterThan(20);
    for (const op of ops) {
      const rendered = operatorCopy(op);
      expect(rendered).toBeTruthy();
      expect(rendered).not.toContain("_");
    }
  });
});

describe("scalar branches of an untagged union", () => {
  /** A condition comparison's `value`: string | number | boolean | {relative}. */
  function valueSchema() {
    const item = deref(deref(deref(SCHEMA.$defs["condition_filter"])?.properties?.["rules"])?.items);
    const comparison = branchAt(item, branchLabels(item).indexOf("Custom field comparison"));
    return deref(comparison?.properties?.["value"]);
  }

  it("matches a plain string, number or boolean operand", () => {
    // Requiring an object here left every scalar operand matching nothing, so
    // the chooser stayed on "Choose…" and rendered no input at all.
    const schema = valueSchema();

    expect(matchBranch(schema, "hello")).toBeGreaterThanOrEqual(0);
    expect(matchBranch(schema, 42)).toBeGreaterThanOrEqual(0);
    expect(matchBranch(schema, true)).toBeGreaterThanOrEqual(0);
  });

  it("routes a whole number to the number branch, not to nothing", () => {
    // Every integer is a number in JSON Schema.
    const schema = valueSchema();
    const branch = branchAt(schema, matchBranch(schema, 42));

    expect(typesOf(branch)).toContain("number");
  });

  it("still matches the object branch it shares the union with", () => {
    const schema = valueSchema();
    const index = matchBranch(schema, { relative: { unit: "days", offset: -7 } });

    expect(index).toBeGreaterThanOrEqual(0);
    expect(branchAt(schema, index)?.properties).toHaveProperty("relative");
  });

  it("gives each kind a distinct branch, so switching actually switches", () => {
    const schema = valueSchema();
    const forString = matchBranch(schema, "hello");
    const forNumber = matchBranch(schema, 42);
    const forBoolean = matchBranch(schema, true);

    expect(new Set([forString, forNumber, forBoolean]).size).toBe(3);
  });
});
