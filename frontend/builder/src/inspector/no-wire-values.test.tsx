/**
 * No dropdown offers a schema value as its label.
 *
 * The Python guards (`apps/common/tests/test_user_facing_copy.py`) check
 * rendered templates, the strings *in* the schema artefact, and validation
 * messages. None of them could see this one, because the artefact legitimately
 * contains `system_field` — it is a valid value of a real enum. The bug was a
 * render site handing that value straight to the page, and it shipped: a
 * question's "Save into" offered `system_field`, a condition offered
 * `has_no_value`, a notification offered `in_app`.
 *
 * So this checks the rendered form rather than the data: mount every node
 * type's editor and assert nothing on screen looks like a wire value.
 */
import { describe, expect, it } from "vitest";

import { NODE_TYPES } from "../schema/artifact";
import { makeDetail, makeSampleGraph } from "../test/fixtures";
import { makeStore, renderWith } from "../test/render";
import { StepEditor } from "../editor/StepEditor";

/**
 * `snake_case`, which is what a wire value looks like and what prose does not.
 *
 * Two letters per segment, so a stray "a_b" in somebody's message text is not
 * enough to fail the suite — and *repeating* segments, because `\b[a-z]+_[a-z]+\b`
 * misses `has_no_value`: an underscore is a word character, so there is no
 * boundary around the middle segment and the anchor never matches.
 */
const WIRE_VALUE = /[a-z]{2,}(?:_[a-z]{2,})+/;

/**
 * Placeholders are shown deliberately and are snake_case by design.
 *
 * `{{ first_name }}` is what the contact's name is called at send time, and the
 * preview column exists to show it unsubstituted — see preview/Preview.tsx. So
 * anything inside braces is removed before the check rather than allowlisted
 * value by value, which would grow a line per placeholder forever.
 */
function withoutPlaceholders(text: string): string {
  return text.replace(/\{\{[^}]*\}\}/g, " ");
}

describe("no dropdown offers a wire value", () => {
  /**
   * Scoped to `<option>` text, deliberately.
   *
   * A first cut swept the whole panel and caught two more things — a WhatsApp
   * template field whose sample reads `template_name/en_US`, and a `flow_id`
   * somewhere in start_flow's editor. Both are sample text rather than labels,
   * which is a different question with a different right answer (a sample that
   * shows the expected format is doing its job), so widening this rule to cover
   * them would have meant weakening it until it asserted very little.
   *
   * Options are the case that shipped, and they have exactly one correct
   * answer: never the wire value.
   */
  it.each(NODE_TYPES.map((spec) => spec.type))("%s", (type) => {
    const store = makeStore(makeDetail(makeSampleGraph({ optional: true })));
    const id = store.getState().nodeOrder.find((entry) => store.getState().nodeType[entry] === type);
    store.getState().setSelection({ nodes: [id as string], edges: [] });

    const { container } = renderWith(store, <StepEditor />);
    const offered = [...container.querySelectorAll("option")]
      .map((option) => withoutPlaceholders(option.textContent ?? ""))
      .filter((text) => WIRE_VALUE.test(text));

    expect({ type, offered }).toEqual({ type, offered: [] });
  });

  it("checks something, so a broken selector cannot pass silently", () => {
    // Guards the guard: if the pattern or the render stopped matching, every
    // case above would pass by finding nothing at all.
    expect(WIRE_VALUE.test("system_field")).toBe(true);
    expect(WIRE_VALUE.test("has_no_value")).toBe(true);
    expect(WIRE_VALUE.test("A field every contact has")).toBe(false);
    expect(withoutPlaceholders("Hi {{ first_name }} there")).not.toMatch(WIRE_VALUE);
  });

  it("finds options at all, so an empty sweep is not a pass", () => {
    const store = makeStore(makeDetail(makeSampleGraph({ optional: true })));
    const id = store.getState().nodeOrder.find((entry) => store.getState().nodeType[entry] === "data_collection");
    store.getState().setSelection({ nodes: [id as string], edges: [] });

    const { container } = renderWith(store, <StepEditor />);

    expect(container.querySelectorAll("option").length).toBeGreaterThan(3);
  });

  it("still submits the wire value the schema asked for", () => {
    // The labels are the reader's half. The document is unchanged.
    const store = makeStore(makeDetail(makeSampleGraph({ optional: true })));
    const config = store.getState().config[
      store.getState().nodeOrder.find((id) => store.getState().nodeType[id] === "data_collection") as string
    ] as { target?: { type?: string } };

    expect(config.target?.type).toMatch(/^[a-z_]+$/);
  });
});
