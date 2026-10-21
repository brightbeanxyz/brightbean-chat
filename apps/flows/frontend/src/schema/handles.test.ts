/**
 * The handle port, checked against the rule its Python original states.
 *
 * A divergence here does not degrade: the server derives its own set on every
 * save and rejects an edge naming a handle it did not derive, so a mismatch
 * breaks *every* save of any graph containing the node.
 */
import { describe, expect, it } from "vitest";

import { NODE_TYPES, nodeSpec } from "./artifact";
import { HANDLE_PATTERN, handleLabel, sourceHandles, sourceHandlesFor } from "./handles";
import { sampleConfig } from "./sample";

const spec = (type: string) => nodeSpec(type) as NonNullable<ReturnType<typeof nodeSpec>>;

describe("sourceHandles", () => {
  it("derives btn: and qr: from the config, in config order", () => {
    const config = {
      blocks: [{ type: "text", text: "hi" }],
      buttons: [{ id: "a", label: "A", action: "postback" }, { id: "b", label: "B", action: "postback" }],
      quick_replies: [{ id: "q", label: "Q" }],
    };

    expect(sourceHandles(spec("send_message"), config)).toEqual([
      "default",
      "timeout",
      "btn:a",
      "btn:b",
      "qr:q",
    ]);
  });

  it("ignores an item whose id is not a string, because the server does", () => {
    // handles_for_node checks `isinstance(item.get("id"), str)`. Drawing a
    // handle here that the validator will not accept means every save of this
    // graph answers handle_not_available.
    const config = { buttons: [{ id: 7, label: "Seven" }, { label: "No id" }, "not an object"] };

    expect(sourceHandles(spec("send_message"), config)).toEqual(["default", "timeout"]);
  });

  it("returns only the static handles for a config that is not an object", () => {
    expect(sourceHandles(spec("condition"), null)).toEqual(["cond:true", "cond:false"]);
    expect(sourceHandles(spec("condition"), "nonsense")).toEqual(["cond:true", "cond:false"]);
  });

  it("gives the randomizer no default handle — every edge out of it is a path", () => {
    const config = { paths: [{ id: "x", weight: 50 }, { id: "y", weight: 50 }] };

    expect(sourceHandles(spec("randomizer"), config)).toEqual(["rand:x", "rand:y"]);
  });

  it("gives a terminal node and an annotation none at all", () => {
    expect(sourceHandles(spec("start_flow"), { flow_id: "f" })).toEqual([]);
    expect(sourceHandles(spec("note"), { text: "hi" })).toEqual([]);
  });

  it("returns nothing for a node type this bundle has never heard of", () => {
    expect(sourceHandlesFor("invented_by_layer_5", { anything: true })).toEqual([]);
  });

  it.each(NODE_TYPES.map((entry) => entry.type))("%s only ever produces well-formed handles", (type) => {
    for (const handle of sourceHandles(spec(type), sampleConfig(type, { optional: true }))) {
      expect(handle).toMatch(HANDLE_PATTERN);
    }
  });
});

describe("handleLabel", () => {
  const config = {
    buttons: [{ id: "a", label: "Book a call", action: "postback" }],
    quick_replies: [{ id: "q", label: "Later" }],
    paths: [{ id: "p", weight: 30 }],
  };

  it.each([
    ["default", ""],
    ["timeout", "Timeout"],
    ["error", "Error"],
    ["cond:true", "Yes"],
    ["cond:false", "No"],
    ["btn:a", "Book a call"],
    ["qr:q", "Later"],
    ["rand:p", "30%"],
  ])("labels %s as %s", (handle, expected) => {
    expect(handleLabel(handle, config)).toBe(expected);
  });

  it("falls back to the id when the config no longer has the item", () => {
    expect(handleLabel("btn:gone", config)).toBe("gone");
  });

  it("renders a prefix from a later layer verbatim rather than losing it", () => {
    expect(handleLabel("newkind:thing", config)).toBe("thing");
  });
});
