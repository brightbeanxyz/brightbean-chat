/**
 * Building a config the server will accept, from the schema alone.
 *
 * Two callers, one implementation:
 *
 * * placing a node from the palette (`minimal`), and
 * * the round-trip fixtures (`maximal`), which fill every optional key and walk
 *   every union branch.
 *
 * Getting the minimal case right matters more than it looks. Every config
 * object in the artefact is closed and most carry `required` and `minLength: 1`,
 * so a node seeded with `{blocks: [{type: "text", text: ""}]}` fails validation
 * — and because autosave fires two seconds after the drop, the user's first
 * experience of the builder would be a red banner about a node they have not
 * touched. Placeholder copy is not decoration here; it is what makes the first
 * save succeed.
 *
 * Nothing in this module is node-type-aware. It reads the schema, so a node type
 * registered by a later layer is placeable the moment `make schema` runs.
 */
import { configSchema } from "./artifact";
import { newItemId } from "./ids";
import {
  anyOfRequirements,
  branchAt,
  constValue,
  deref,
  isTaggedUnion,
  isUntaggedUnion,
  typesOf,
  variantChoices,
  variantSchema,
} from "./resolve";
import type { JsonSchema } from "./types";

export interface SampleOptions {
  /** Fill optional properties too. The fixtures do; a placed node does not. */
  optional?: boolean;
  /** Which union branch to take, by index into the discriminator mapping. */
  variant?: number;
  /** Injected so fixtures can be deterministic. */
  makeId?: () => string;
}

/**
 * A string that satisfies `pattern`.
 *
 * Only the patterns the artefact actually uses are handled. An unknown one
 * throws rather than returning something invalid: this function's whole job is
 * producing configs that pass, and a silent near-miss would surface as a 422
 * from a node the user never edited.
 *
 * A pattern added on the server side lands here as a failing test rather than a
 * broken panel, which is ROADMAP contract 2 working: the schema module is the
 * single source of truth and "changing entries requires touching both
 * consumers" is enforced by this throw.
 */
function stringForPattern(pattern: string, hint: string): string {
  switch (pattern) {
    case "^([01][0-9]|2[0-3]):[0-5][0-9]$":
      return "09:00";
    case "^[A-Za-z0-9_-]{1,64}$":
      return hint.replace(/[^A-Za-z0-9_-]/g, "-").slice(0, 64) || "id";
    case "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$":
      // Contract 8 addresses tags, fields, segments and sequences by their
      // real primary keys. A placed condition therefore starts pointing at
      // nothing, and the panel's pick-lists are how it gets filled in.
      return "00000000-0000-0000-0000-000000000000";
    case "^(?:default|timeout|error|cond:(?:true|false)|(?:btn|qr|rand):[A-Za-z0-9_-]{1,64})$":
      return "default";
    case "^[a-z0-9_]{1,512}/[A-Za-z_]{2,10}$":
      // A WhatsApp template reference, `<name>/<language>` (issue #19). Like
      // the uuid above, a placed node starts pointing at nothing real and the
      // panel's template picker is how it gets filled in — so the placeholder
      // only has to be well-formed, not resolvable.
      return "template_name/en_US";
    case "^(header|body|button\\.[0-9]{1,2})\\.[0-9]{1,3}$":
      // Which of a template's numbered slots this value fills. The first body
      // slot is the one every template with variables has.
      return "body.1";
    default:
      throw new Error(`No sample value known for pattern ${pattern} — teach schema/sample.ts about it.`);
  }
}

/**
 * Readable placeholder copy for a free-text field, from its property name.
 *
 * Exported because the form's `defaultFor` needs the same answer: a button
 * added in the panel and a button in a placed node should read alike, and
 * neither should read "…".
 */
export function placeholderFor(key: string, schema: JsonSchema): string {
  if (schema.pattern) {
    return stringForPattern(schema.pattern, key);
  }
  const copy: Record<string, string> = {
    text: "New message",
    label: "Button",
    question: "What is your answer?",
    subject: "Subject",
    html_body: "<p>Hello {{first_name}}</p>",
    url: "https://example.com",
    media_url: "https://example.com/file",
    caption: "Caption",
    title: "Title",
    subtitle: "Subtitle",
    image: "https://example.com/image.png",
    tag: "tag",
    field: "field",
    value: "value",
    sequence: "sequence",
    member: "member",
    name: "X-Example",
    json_path: "$.result",
    target: "field",
    key: "field",
    flow_id: "",
    invalid_text: "That did not look right — try again.",
  };
  const candidate = copy[key] ?? "Text";
  const max = schema.maxLength;
  const trimmed = max !== undefined && candidate.length > max ? candidate.slice(0, max) : candidate;
  // A minLength of 1 with an empty placeholder is the failure this module
  // exists to prevent, so never return "" for a field that forbids it.
  return trimmed.length === 0 && (schema.minLength ?? 0) > 0 ? key.slice(0, max ?? 64) || "value" : trimmed;
}

function numberFor(schema: JsonSchema): number {
  const min = schema.minimum ?? 0;
  const max = schema.maximum;
  if (max !== undefined && min > max) {
    return max;
  }
  return min === 0 ? (max !== undefined && max < 1 ? max : 1) : min;
}

/**
 * Which side of an "a or b" requirement to fill in.
 *
 * Prefer a branch that is not an id reference. An invented `media_id` is a
 * well-formed pointer to an asset that does not exist — it validates, so
 * nothing catches it, and the flow fails at send time. An invented URL is
 * visibly a placeholder and fails honestly.
 */
export function preferredBranch(groups: string[][]): string[] {
  if (groups.length === 0) {
    return [];
  }
  const notAnId = groups.find((group) => group.every((key) => !key.endsWith("_id")));
  return notAnId ?? (groups[0] as string[]);
}

function sampleValue(schema: JsonSchema | undefined, key: string, options: Required<SampleOptions>): unknown {
  const resolved = deref(schema);
  if (!resolved) {
    return undefined;
  }

  const pinned = constValue(resolved);
  if (pinned !== undefined) {
    return pinned;
  }

  if (isUntaggedUnion(resolved)) {
    const branches = resolved.oneOf ?? [];
    if (branches.length === 0) {
      return {};
    }
    // Recurse rather than assuming the branch is an object: a condition rule's
    // `value` is itself an untagged union whose branches are a string, a
    // number, a boolean and a relative-date object.
    return sampleValue(branchAt(resolved, options.variant % branches.length), key, options);
  }

  if (isTaggedUnion(resolved)) {
    const tags = variantChoices(resolved);
    const tag = tags[options.variant % Math.max(tags.length, 1)];
    if (tag === undefined) {
      return {};
    }
    const branch = variantSchema(resolved, tag);
    const value = sampleObject(branch, options) as Record<string, unknown>;
    const property = resolved.discriminator?.propertyName;
    if (property) {
      value[property] = tag;
    }
    return value;
  }

  if (resolved.enum && resolved.enum.length > 0) {
    return resolved.enum[options.variant % resolved.enum.length];
  }

  const types = typesOf(resolved);

  // `external_request.body` is `{}` — any JSON at all. An empty object is the
  // least surprising thing to seed, and is valid.
  if (types.length === 0 && !resolved.properties && !resolved.oneOf) {
    return {};
  }

  if (types.includes("array")) {
    const count = Math.max(resolved.minItems ?? 0, options.optional ? Math.max(resolved.minItems ?? 0, 1) : 0);
    return Array.from({ length: count }, (_unused, index) =>
      sampleValue(resolved.items, key, { ...options, variant: options.variant + index }),
    );
  }

  if (types.includes("object") || resolved.properties) {
    return sampleObject(resolved, options);
  }

  if (types.includes("string")) {
    // An `id` property backs a dynamic handle, so it must be unique per item.
    return key === "id" ? options.makeId() : placeholderFor(key, resolved);
  }
  if (types.includes("integer") || types.includes("number")) {
    return numberFor(resolved);
  }
  if (types.includes("boolean")) {
    return true;
  }
  // A scalar union (`condition` rule values are string|number|boolean|null).
  return placeholderFor(key, resolved);
}

function sampleObject(schema: JsonSchema | undefined, options: Required<SampleOptions>): unknown {
  const resolved = deref(schema);
  if (!resolved?.properties) {
    return {};
  }

  const required = new Set(resolved.required ?? []);
  // An anyOf ("media_id or url") has to be satisfied, because a config meeting
  // neither branch is a document-stage error — and those discard the whole
  // save, not just the offending node. `preferredBranch` picks which one.
  const branch = preferredBranch(anyOfRequirements(resolved));
  for (const key of branch) {
    required.add(key);
  }

  const out: Record<string, unknown> = {};
  for (const [key, property] of Object.entries(resolved.properties)) {
    const pinned = constValue(deref(property));
    if (pinned !== undefined) {
      out[key] = pinned;
      continue;
    }
    if (!options.optional && !required.has(key)) {
      continue;
    }
    const value = sampleValue(property, key, options);
    if (value !== undefined) {
      out[key] = value;
    }
  }
  return out;
}

/** A config for `type` that the server will accept. */
export function sampleConfig(type: string, options: SampleOptions = {}): unknown {
  const schema = configSchema(type);
  if (!schema) {
    return {};
  }
  const resolved = { optional: false, variant: 0, makeId: newItemId, ...options };
  const value = sampleValue(schema, "config", resolved);
  return value === undefined ? {} : value;
}

/** The config a node dropped from the palette starts with. */
export function newNodeConfig(type: string): unknown {
  return sampleConfig(type, { optional: false });
}
