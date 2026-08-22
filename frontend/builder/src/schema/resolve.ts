/**
 * The JSON Schema subset the artefact actually uses, resolved.
 *
 * Deliberately not a general-purpose implementation: it handles local `$ref`,
 * `oneOf` + `discriminator`, and the one `anyOf` shape the artefact carries.
 * Anything else it does not understand it says so about, rather than guessing —
 * a form that silently renders the wrong widget produces a config the server
 * rejects, and the user sees a 422 instead of a validation message.
 */
import { SCHEMA } from "./artifact";
import type { JsonSchema } from "./types";

const DEFS_PREFIX = "#/$defs/";

/** The `$defs` name a local `$ref` points at, or `undefined`. */
export function defsName(schema: JsonSchema | undefined): string | undefined {
  if (!schema?.$ref?.startsWith(DEFS_PREFIX)) {
    return undefined;
  }
  return schema.$ref.slice(DEFS_PREFIX.length);
}

/**
 * Follow `$ref` until the schema is something renderable.
 *
 * Only local `#/$defs/<name>` refs exist in the artefact. A remote one would be
 * a change to export.py that this module has to be taught about, so it throws
 * rather than rendering an empty form.
 */
export function deref(schema: JsonSchema | undefined, depth = 0): JsonSchema | undefined {
  if (!schema || !schema.$ref) {
    return schema;
  }
  if (depth > 20) {
    throw new Error(`Circular $ref chain at ${schema.$ref}`);
  }
  const name = defsName(schema);
  if (name === undefined) {
    throw new Error(`Unsupported $ref ${schema.$ref} — only local #/$defs/ refs are handled.`);
  }
  const target = SCHEMA.$defs[name];
  if (!target) {
    throw new Error(`$ref ${schema.$ref} names a $defs entry that does not exist.`);
  }
  return deref(target, depth + 1);
}

export function isTaggedUnion(schema: JsonSchema | undefined): boolean {
  return Boolean(schema?.oneOf && schema.discriminator);
}

/**
 * A `oneOf` with no discriminator.
 *
 * Contract 8's condition filter is built this way: eight rule shapes, one per
 * source, each pinning `source` as a `const` and constraining `op` to the
 * operators that source actually supports. Two sources contribute two branches
 * each — presence versus comparison — so `source` alone does not name a branch
 * and there is no single property that could be a discriminator.
 *
 * That is a better encoding than a tagged union would be, because it means the
 * source-to-operator mapping lives in the schema rather than in a table this
 * bundle would have to keep in step with apps/contacts/conditions.py.
 */
export function isUntaggedUnion(schema: JsonSchema | undefined): boolean {
  return Boolean(schema?.oneOf && !schema.discriminator);
}

/** The properties a branch pins to a literal, which is what identifies it. */
export function constProperties(branch: JsonSchema | undefined): Record<string, unknown> {
  const pinned: Record<string, unknown> = {};
  for (const [key, property] of Object.entries(branch?.properties ?? {})) {
    const value = constValue(deref(property));
    if (value !== undefined) {
      pinned[key] = value;
    }
  }
  return pinned;
}

/** Human labels for an untagged union's branches, in schema order. */
export function branchLabels(schema: JsonSchema | undefined): string[] {
  return (schema?.oneOf ?? []).map((branch, index) => {
    const resolved = deref(branch);
    if (resolved?.title) {
      return resolved.title;
    }
    const pinned = Object.values(constProperties(resolved));
    return pinned.length > 0 ? String(pinned[0]) : `Option ${index + 1}`;
  });
}

/**
 * Which branch a value belongs to, by index, or -1.
 *
 * Matched on three things, because no one of them is enough: every pinned
 * literal has to agree (that rules out the wrong source), every required key
 * has to be present, and the value may carry no key the branch does not
 * declare — every object here is closed, so an extra key means a different
 * branch. That last clause is what separates "Custom field" from "Custom field
 * comparison", which differ only by `value`.
 */
export function matchBranch(schema: JsonSchema | undefined, value: unknown): number {
  const branches = schema?.oneOf ?? [];

  return branches.findIndex((candidate) => {
    const branch = deref(candidate);
    if (!branch) {
      return false;
    }

    // A branch can be a scalar. A condition comparison's `value` is
    // string | number | boolean | {relative}, so requiring an object here left
    // every scalar operand matching nothing — the chooser stayed on "Choose…"
    // and rendered no input at all, making the operand uneditable.
    if (!branch.properties && typesOf(branch).length > 0) {
      return acceptsScalar(typesOf(branch), value);
    }

    if (typeof value !== "object" || value === null || Array.isArray(value)) {
      return false;
    }
    const record = value as Record<string, unknown>;

    for (const [key, pinned] of Object.entries(constProperties(branch))) {
      if (record[key] !== pinned) {
        return false;
      }
    }
    if ((branch.required ?? []).some((key) => record[key] === undefined)) {
      return false;
    }
    const declared = new Set(Object.keys(branch.properties ?? {}));
    return Object.keys(record).every((key) => declared.has(key));
  });
}

/** A value's JSON Schema type name. */
export function jsonTypeOf(value: unknown): string {
  if (value === null) {
    return "null";
  }
  if (Array.isArray(value)) {
    return "array";
  }
  if (typeof value === "number") {
    return Number.isInteger(value) ? "integer" : "number";
  }
  return typeof value;
}

/** Whether a scalar branch's declared types accept this value. */
export function acceptsScalar(types: readonly string[], value: unknown): boolean {
  const actual = jsonTypeOf(value);
  // Every integer is a number in JSON Schema, so a `number` branch has to
  // accept 5 — otherwise a whole-number operand matches no branch and the
  // field renders nothing.
  return types.includes(actual) || (actual === "integer" && types.includes("number"));
}

/** The resolved branch a value belongs to, or `undefined`. */
export function branchFor(schema: JsonSchema | undefined, value: unknown): JsonSchema | undefined {
  const index = matchBranch(schema, value);
  return index === -1 ? undefined : deref(schema?.oneOf?.[index]);
}

/** One branch by index, resolved. */
export function branchAt(schema: JsonSchema | undefined, index: number): JsonSchema | undefined {
  return deref(schema?.oneOf?.[index]);
}

/**
 * Tags a person is more likely to want first.
 *
 * `export.py` serialises with `sort_keys=True`, so `discriminator.mapping`
 * arrives alphabetical — which makes "audio" the first block kind and "add a
 * tag" the first action. That is an artefact of the serialiser, not a
 * statement about what a new message should be, so these few are floated to
 * the front. It is presentation only: the set still comes entirely from the
 * schema, and a tag not listed here keeps its place.
 */
const PREFERRED_TAGS = ["text", "duration", "url"];

/**
 * The tags a tagged union offers, most-useful-first.
 *
 * Read from `discriminator.mapping` and NOT from the `oneOf` branches: they are
 * not one-to-one. `message_block` has seven tags (text, image, audio, video,
 * file, card, gallery) over four branches, because image/audio/video/file all
 * map to `block_media`. Enumerating branches would silently drop three block
 * kinds from the picker.
 */
export function variantChoices(schema: JsonSchema | undefined): string[] {
  const tags = schema?.discriminator ? Object.keys(schema.discriminator.mapping) : [];
  const preferred = PREFERRED_TAGS.filter((tag) => tags.includes(tag));
  return [...preferred, ...tags.filter((tag) => !preferred.includes(tag))];
}

/** The branch schema for one tag, resolved. */
export function variantSchema(schema: JsonSchema | undefined, tag: string): JsonSchema | undefined {
  const ref = schema?.discriminator?.mapping[tag];
  return ref === undefined ? undefined : deref({ $ref: ref });
}

/** The branch matching a value's current tag, or `undefined` when it has none. */
export function variantFor(schema: JsonSchema | undefined, value: unknown): JsonSchema | undefined {
  const property = schema?.discriminator?.propertyName;
  if (!property || typeof value !== "object" || value === null) {
    return undefined;
  }
  const tag = (value as Record<string, unknown>)[property];
  return typeof tag === "string" ? variantSchema(schema, tag) : undefined;
}

/**
 * The alternative required-key sets an `anyOf` demands.
 *
 * Recognises exactly the `anyOf: [{required: [a]}, {required: [b]}]` shape used
 * by `block_media` ("media_id or url") and `smart_delay_date.date` ("field or
 * datetime"). Anything else returns `[]`, which the renderer reads as "no extra
 * requirement" — the server still enforces it, so the cost is a late error
 * rather than a wrong one.
 */
export function anyOfRequirements(schema: JsonSchema | undefined): string[][] {
  if (!schema?.anyOf) {
    return [];
  }
  const groups = schema.anyOf.map((branch) => branch.required ?? []);
  return groups.every((group) => group.length > 0) ? groups : [];
}

/** Whether a resolved schema is a plain object with named properties. */
export function isObjectSchema(schema: JsonSchema | undefined): boolean {
  return Boolean(schema?.properties) || schema?.type === "object";
}

/** `type` as a set, since the artefact uses both a string and an array form. */
export function typesOf(schema: JsonSchema | undefined): string[] {
  const type = schema?.type;
  if (type === undefined) {
    return [];
  }
  return Array.isArray(type) ? type : [type];
}

/** Whether the schema pins the value to a single literal (a discriminator tag). */
export function constValue(schema: JsonSchema | undefined): unknown {
  return schema && "const" in schema ? schema.const : undefined;
}
