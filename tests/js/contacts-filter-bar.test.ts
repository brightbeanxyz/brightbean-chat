/**
 * The contact filter builder's round-trip, against the condition engine's own
 * vocabulary.
 *
 * `apps/contacts/tests/test_crm.py` proves the server half of issue #13's
 * headline acceptance criterion: save a filter, reload it, and the stored rules
 * are identical. This is the other half — hydrate a stored document into the
 * editor and serialise it back — because the two halves are what "round-trip"
 * actually means, and a client that normalised a relative date on the way in
 * would pass every Python test and still show an operator different rules than
 * they saved.
 *
 * The component under test lives in a Django template rather than a module: it
 * is fifty lines of Alpine glue for one page, and a build step for it would mean
 * a second bundle, a second entry point and a second thing that can be stale.
 * So the test reads the template, pulls the <script> out and evaluates it — which
 * also means the file it checks is literally the file the browser gets.
 *
 * The vocabulary comes from `static/flows/flow-schema.json`'s condition schema,
 * which is generated from apps/contacts/conditions.py and committed (the
 * Makefile regenerates it and a Python test fails when the committed copy is
 * stale). So this test cannot pass against an operator table that has drifted
 * from the engine's.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

const REPO = resolve(import.meta.dirname, "../..");

type Rule = Record<string, unknown>;
type Doc = { match: string; rules: Rule[] };

/** The `contactFilters` factory, evaluated out of the template it ships in. */
function loadFactory(): (config: Record<string, unknown>) => any {
  const template = readFileSync(
    resolve(REPO, "templates/contacts/_filter_bar_script.html"),
    "utf8",
  );
  // Anchored on the nonce attribute and non-greedy: the file also carries a
  // Django comment, and a bare /<script[^>]*>([\s\S]*)<\/script>/ would happily
  // start at any script tag someone mentions in prose there.
  const script = /<script[^>]*\bnonce=[^>]*>([\s\S]*?)<\/script>/.exec(template);
  if (!script) throw new Error("no nonced script tag in _filter_bar_script.html");
  return new Function(`${script[1]}; return contactFilters;`)();
}

/** The condition vocabulary, from the artefact the flow builder already uses. */
function vocabulary(): Record<string, any> {
  const schema = JSON.parse(
    readFileSync(resolve(REPO, "static/flows/flow-schema.json"), "utf8"),
  );
  const found = findConditionExtension(schema);
  if (!found) throw new Error("no x-brightbean condition block in the schema artefact");
  return found;
}

/** The condition schema is embedded somewhere inside the node schema. */
function findConditionExtension(node: unknown): Record<string, any> | null {
  if (Array.isArray(node)) {
    for (const child of node) {
      const found = findConditionExtension(child);
      if (found) return found;
    }
    return null;
  }
  if (node && typeof node === "object") {
    const record = node as Record<string, unknown>;
    const extension = record["x-brightbean"] as Record<string, any> | undefined;
    if (extension && extension.opsByType && extension.valuelessOps) return extension;
    for (const value of Object.values(record)) {
      const found = findConditionExtension(value);
      if (found) return found;
    }
  }
  return null;
}

const TAG = "11111111-1111-1111-1111-111111111111";
const FIELD = "22222222-2222-2222-2222-222222222222";
const SEQUENCE = "33333333-3333-3333-3333-333333333333";

const contactFilters = loadFactory();
const vocab = vocabulary();

function build(document: Partial<Doc>) {
  return contactFilters({
    vocabulary: vocab,
    sources: [
      { name: "tag", label: "Tag", keyKind: "uuid", evaluable: true, owner: "" },
      { name: "custom_field", label: "Custom field", keyKind: "uuid", evaluable: true, owner: "" },
      { name: "system_field", label: "Contact field", keyKind: "system_field", evaluable: true, owner: "" },
      { name: "segment", label: "Segment", keyKind: "uuid", evaluable: true, owner: "" },
      { name: "sequence", label: "Sequence", keyKind: "uuid", evaluable: true, owner: "issue #22, L6-A" },
      { name: "window", label: "Messaging window", keyKind: "platform", evaluable: true, owner: "" },
    ],
    platforms: [{ value: "telegram", label: "Telegram" }],
    tags: [{ value: TAG, label: "VIP" }],
    fields: [{ value: FIELD, label: "Plan", type: "text" }],
    segments: [],
    sequences: [{ value: SEQUENCE, label: "Onboarding" }],
    document,
    segmentId: "",
  });
}

describe("hydrate then serialise", () => {
  const cases: Array<[string, Doc]> = [
    ["a tag rule", { match: "all", rules: [{ source: "tag", key: TAG, op: "has" }] }],
    ["a negated tag rule", { match: "all", rules: [{ source: "tag", key: TAG, op: "has_not" }] }],
    [
      "a text comparison",
      { match: "any", rules: [{ source: "system_field", key: "email", op: "contains", value: "example.test" }] },
    ],
    [
      "a relative date",
      {
        match: "all",
        rules: [
          {
            source: "system_field",
            key: "created_at",
            op: "after",
            value: { relative: { unit: "days", offset: -7 } },
          },
        ],
      },
    ],
    [
      "an absolute date",
      { match: "all", rules: [{ source: "system_field", key: "created_at", op: "before", value: "2026-01-31" }] },
    ],
    [
      "a custom field",
      { match: "all", rules: [{ source: "custom_field", key: FIELD, op: "is", value: "Pro" }] },
    ],
    [
      "a valueless custom-field operator",
      { match: "all", rules: [{ source: "custom_field", key: FIELD, op: "no_value" }] },
    ],
    [
      "several rules at once",
      {
        match: "any",
        rules: [
          { source: "tag", key: TAG, op: "has_not" },
          { source: "system_field", key: "first_name", op: "has_value" },
          { source: "system_field", key: "email", op: "is", value: "ada@example.test" },
        ],
      },
    ],
  ];

  it.each(cases)("%s survives unchanged", (_name, document) => {
    expect(JSON.parse(build(document).serialised)).toEqual(document);
  });
});

describe("what the builder refuses to send", () => {
  it("serialises an empty builder to the empty string, not a match-everyone document", () => {
    // `{"match":"all","rules":[]}` is the identity of AND and therefore matches
    // the whole workspace. It is the same *set* as no filter, but only the empty
    // string lets the view tell "no filter" from "a filter that happens to be
    // empty" — and keeps it out of the URL.
    expect(build({}).serialised).toBe("");
  });

  it("drops a half-built rule rather than shipping one the server would reject", () => {
    const editor = build({});
    editor.addRule();

    expect(editor.serialised).toBe("");
  });

  it("drops a comparison whose value is still blank", () => {
    const editor = build({ match: "all", rules: [{ source: "system_field", key: "email", op: "is", value: "x" }] });
    editor.rules[0].value = "";

    expect(editor.serialised).toBe("");
  });

  it("strips a stray value when the operator stops taking one", () => {
    // The schema's valueless branch sets additionalProperties:false, so a rule
    // that kept its value would be rejected outright rather than ignored.
    const editor = build({
      match: "all",
      rules: [{ source: "system_field", key: "email", op: "is", value: "x@y.test" }],
    });
    editor.rules[0].op = "has_value";
    editor.touch();

    expect(JSON.parse(editor.serialised).rules[0]).toEqual({
      source: "system_field",
      key: "email",
      op: "has_value",
    });
  });
});

describe("the vocabulary is the engine's, not a copy", () => {
  it("offers the operators the schema lists for a field's type", () => {
    const editor = build({});
    const rule = { source: "custom_field", key: FIELD, op: "", value: "", mode: "on", offset: 0 };

    expect(editor.opOptions(rule)).toEqual(vocab.opsByType.text);
  });

  it("offers a source's native operators when it has them", () => {
    const editor = build({});
    const rule = { source: "tag", key: TAG, op: "", value: "", mode: "on", offset: 0 };

    expect(editor.opOptions(rule)).toEqual(vocab.opsBySource.tag);
  });

  it("keeps the operator when a new key still allows it", () => {
    // Changing "Email is X" to "Phone is X" should not silently reset to the
    // first operator in the list.
    const editor = build({ match: "all", rules: [{ source: "system_field", key: "email", op: "contains", value: "x" }] });
    editor.rules[0].key = "phone";
    editor.onKeyChange(editor.rules[0]);

    expect(editor.rules[0].op).toBe("contains");
  });

  it("resets the operator when the new key does not allow it", () => {
    const editor = build({ match: "all", rules: [{ source: "system_field", key: "email", op: "contains", value: "x" }] });
    editor.rules[0].key = "created_at";
    editor.onKeyChange(editor.rules[0]);

    expect(vocab.opsByType.datetime).toContain(editor.rules[0].op);
  });

  it("offers the workspace's sequences as the sequence source's keys", () => {
    const editor = build({});

    expect(editor.keyOptions({ source: "sequence", key: "" })).toEqual([
      { value: SEQUENCE, label: "Onboarding" },
    ]);
  });

  it("reports no key options for a source with no picker", () => {
    const editor = build({});

    expect(editor.keyOptions({ source: "not_a_source", key: "" })).toEqual([]);
  });
});
