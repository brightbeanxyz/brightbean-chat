import { describe, expect, it } from "vitest";

import { SCHEMA, configSchema } from "./artifact";
import { anyOfRequirements, deref, isTaggedUnion, typesOf, variantChoices, variantFor, variantSchema } from "./resolve";

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
