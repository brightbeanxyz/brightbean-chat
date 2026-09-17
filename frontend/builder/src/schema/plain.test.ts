/**
 * The plain-language phrases stay in step with the registry.
 *
 * `plain.ts` lives here rather than on the Python `NodeSpec`, which buys a copy
 * tweak with no `make schema` and no artefact re-commit — and costs the risk of
 * the two drifting. These tests are that cost paid: they are generated from the
 * artefact, so a group registered in Python with no phrase here is a red build
 * naming the group, rather than a blank eyebrow nobody notices on a canvas.
 */

import { describe, expect, it } from "vitest";

import { GROUPS, NODE_TYPES, nodeSpec } from "./artifact";
import { GROUP_PHRASE, TYPE_PHRASE, plainKind } from "./plain";

describe("plain-language node kinds", () => {
  it.each(NODE_TYPES.map((spec) => spec.type))(
    "%s resolves to a phrase",
    (type) => {
      const phrase = plainKind(nodeSpec(type), type);

      expect(phrase).toBeTruthy();
      expect(phrase.trim()).toBe(phrase);
    },
  );

  it.each(GROUPS.map((group) => group.key))(
    "the %s group has copy of its own",
    (key) => {
      expect(GROUP_PHRASE[key]).toBeTruthy();
    },
  );

  it("falls back rather than rendering nothing for an unregistered type", () => {
    // The branch a node type added by a later layer lands in before anybody
    // writes copy for it. "Also" is wrong in register and right in substance,
    // which beats an empty line on a card.
    expect(plainKind(undefined, "something_new")).toBe(GROUP_PHRASE.other);
  });

  it("overrides the group where the group's phrase would be nonsense", () => {
    // smart_delay and start_flow are both `logic`, and "Then decide" is
    // wrong for either — one waits, the other hands over.
    for (const type of Object.keys(TYPE_PHRASE)) {
      const spec = nodeSpec(type);
      if (!spec) continue;
      expect(plainKind(spec, type)).toBe(TYPE_PHRASE[type]);
    }
  });

  it("names the moment rather than the mechanism", () => {
    // The registry's own labels are developer copy — "Send Message", "Smart
    // Delay". A phrase that merely echoes one has not done the job.
    //
    // Annotations are the exception, and a real one: a note is not a step, so
    // there is no moment to name and "Note" is exactly the right word. The
    // carve-out is on `spec.annotation` rather than on the type, so a second
    // annotation type would be covered by it too.
    for (const spec of NODE_TYPES) {
      if (spec.annotation) continue;
      expect(plainKind(spec, spec.type).toLowerCase()).not.toBe(
        spec.label.toLowerCase(),
      );
    }
  });
});
