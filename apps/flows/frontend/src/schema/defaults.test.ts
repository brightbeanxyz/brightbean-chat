import { describe, expect, it } from "vitest";

import { validateNode } from "../test/ajv";
import { newNodeId } from "./ids";
import { anyOfRequirements, deref, variantChoices } from "./resolve";
import { SCHEMA } from "./artifact";
import { defaultFor } from "../inspector/SchemaField";
import { newNodeConfig, preferredBranch, sampleConfig } from "./sample";

describe("what a node placed from the palette actually contains", () => {
  it("gives a new message a text block, not whichever kind sorts first", () => {
    // export.py serialises with sort_keys=True, so the discriminator mapping
    // arrives alphabetical and "audio" would otherwise win.
    const config = newNodeConfig("send_message") as { blocks: { type: string }[] };

    expect(config.blocks[0]?.type).toBe("text");
  });

  it("offers text first in the block picker for the same reason", () => {
    expect(variantChoices(deref(SCHEMA.$defs["message_block"]))[0]).toBe("text");
  });

  it("gives a new delay a duration rather than a fixed date", () => {
    expect((newNodeConfig("smart_delay") as { mode: string }).mode).toBe("duration");
  });

  it("never invents a media_id, because an invented one validates and then fails at send", () => {
    const media = deref(SCHEMA.$defs["block_media"]);
    expect(anyOfRequirements(media)).toEqual([["media_id"], ["url"]]);
    expect(preferredBranch(anyOfRequirements(media))).toEqual(["url"]);

    const block = sampleConfig("send_message", { optional: false, variant: 1 });
    expect(JSON.stringify(block)).not.toContain('"media_id"');
  });

  it("still validates once a media block is seeded", () => {
    const config = { blocks: [{ type: "image", url: "https://example.com/image.png" }] };
    expect(validateNode({ id: newNodeId(), type: "send_message", position: { x: 0, y: 0 }, config }).errors).toEqual([]);
  });
});

describe("minting an item id", () => {
  it("uses the item-id grammar where the schema allows it", () => {
    const config = newNodeConfig("randomizer") as { paths: { id: string }[] };

    for (const path of config.paths) {
      expect(path.id).toMatch(/^[A-Za-z0-9_-]{1,64}$/);
    }
  });

  it("does not mint one where the schema demands a different shape", () => {
    // A node type added later with a UUID-shaped `id` would get an 8-character
    // token — a pattern failure, which is document-stage and discards the whole
    // save rather than just that item.
    const uuid = "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$";
    const seeded = defaultFor(
      { type: "object", required: ["id"], properties: { id: { type: "string", pattern: uuid } } },
      "thing",
    ) as { id: string };

    expect(seeded.id).toMatch(new RegExp(uuid));
  });
});
