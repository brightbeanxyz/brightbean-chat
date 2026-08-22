/**
 * The recursive dispatcher: one JSON Schema in, one editable form out.
 *
 * The whole panel — not just its leaves — goes through here. That is what makes
 * `condition` (whose config is a bare `$ref`) and `smart_delay` (whose config is
 * a bare tagged union) work with no special case: two of the eleven node types
 * have no `config.properties` at all, and a renderer that reached for it would
 * break on both.
 *
 * Widget overrides are looked up before the generic kind, so a specific path or
 * a specific `$defs` shape can take over while everything else falls back to
 * the generated form. That is the seam that lets a node type registered by a
 * later layer be configurable with zero code, and lets this one be pleasant.
 */
import {
  anyOfRequirements,
  branchAt,
  branchLabels,
  constValue,
  deref,
  defsName,
  isTaggedUnion,
  isUntaggedUnion,
  matchBranch,
  typesOf,
  variantChoices,
  variantFor,
} from "../schema/resolve";
import { placeholderFor, preferredBranch } from "../schema/sample";
import { newItemId } from "../schema/ids";
import { ID_PATTERN } from "../schema/handles";
import type { JsonSchema } from "../schema/types";
import { formatPath, type ConfigPath } from "../store/paths";
import { useField } from "./FieldContext";
import { labelFor, variantLabel } from "./copy";
import { JsonField, NumberField, ScalarField, SelectField, TextField, ToggleField, fieldId, type FieldProps } from "./fields";
import { lookupOverride } from "./overrides";

export interface SchemaFieldProps {
  /** Possibly a `$ref`; resolved here so overrides can key on the `$defs` name. */
  schema: JsonSchema | undefined;
  path: ConfigPath;
  value: unknown;
  propertyName: string;
  required?: boolean;
  /** The object this field is a property of; see FieldProps.parent. */
  parent?: unknown;
  /**
   * The `$defs` name of the object this field is a property of.
   *
   * Threaded rather than derived, because a property's own schema is usually
   * inline — `block_text.text` is `{type: "string", maxLength: 4096}`, with
   * nothing in it to say it belongs to `block_text`. Without the parent's name
   * a shape-scoped override like `$block_text.text` could never match.
   */
  parentDefs?: string | undefined;
  /**
   * The caller has already labelled this value.
   *
   * An array item sits in a numbered subgroup, so repeating the list's own
   * label above its fields reads as a second, empty field.
   */
  hideLabel?: boolean;
}

export function SchemaField({
  schema,
  path,
  value,
  propertyName,
  required = false,
  parentDefs,
  parent,
  hideLabel = false,
}: SchemaFieldProps) {
  const field = useField();
  const resolved = deref(schema);

  if (!resolved) {
    return null;
  }

  // A `const` is a discriminator tag. It is written when the variant is chosen
  // and there is nothing for a person to decide, so it is not rendered.
  if (constValue(resolved) !== undefined) {
    return null;
  }

  const props: FieldProps = { schema: resolved, path, value, propertyName, required, parent };
  const selfDefs = defsName(schema);

  const Override = lookupOverride({ nodeType: field.nodeType, path, selfDefs, parentDefs, propertyName });
  if (Override) {
    return <Override {...props} />;
  }

  if (isTaggedUnion(resolved)) {
    return <VariantField {...props} rawSchema={resolved} hideLabel={hideLabel} />;
  }
  if (isUntaggedUnion(resolved)) {
    return <UnionField {...props} rawSchema={resolved} hideLabel={hideLabel} />;
  }
  if (resolved.enum && resolved.enum.length > 0) {
    return <SelectField {...props} />;
  }

  const types = typesOf(resolved);

  if (types.includes("array")) {
    return <ArrayField {...props} />;
  }
  if (types.includes("object") || resolved.properties) {
    return <ObjectField {...props} hideLabel={hideLabel} />;
  }
  if (types.includes("boolean") && types.length === 1) {
    return <ToggleField {...props} />;
  }
  if ((types.includes("integer") || types.includes("number")) && types.length === 1) {
    return <NumberField {...props} />;
  }
  if (types.includes("string") && types.length === 1) {
    return <TextField {...props} />;
  }
  if (types.length > 1) {
    return <ScalarField {...props} />;
  }
  // No `type` and no `properties` is the schema's way of saying "any JSON".
  return <JsonField {...props} />;
}

/** A named-property object: required fields first, then the optional ones. */
export function ObjectField({
  schema,
  path,
  value,
  propertyName,
  required,
  selfDefs,
  hideLabel = false,
}: FieldProps & { selfDefs?: string | undefined; hideLabel?: boolean }) {
  const { set, clear, readOnly } = useField();
  const properties = schema.properties ?? {};
  const requiredKeys = new Set(schema.required ?? []);
  const record = (typeof value === "object" && value !== null ? value : {}) as Record<string, unknown>;

  // "media_id or url" and "field or datetime": rendered as a group message so
  // the alternative is visible before the server says so.
  const alternatives = anyOfRequirements(schema);
  const unmet =
    alternatives.length > 0 && !alternatives.some((group) => group.every((key) => record[key] !== undefined));

  const entries = Object.entries(properties).filter(([, property]) => constValue(deref(property)) === undefined);
  const ordered = [
    ...entries.filter(([key]) => requiredKeys.has(key)),
    ...entries.filter(([key]) => !requiredKeys.has(key)),
  ];

  const body = ordered.map(([key, property]) => {
    const childPath = [...path, key];
    const present = record[key] !== undefined;
    const optional = !requiredKeys.has(key);

    if (optional && !present) {
      return readOnly ? null : (
        <button
          key={key}
          type="button"
          className="btn-link text-xs block mb-1"
          onClick={() => set(childPath, defaultFor(property, key), `add:${formatPath(childPath)}`)}
        >
          + {labelFor(key, deref(property)?.title)}
        </button>
      );
    }

    return (
      <div key={key}>
        <SchemaField
          schema={property}
          path={childPath}
          value={record[key]}
          propertyName={key}
          required={!optional}
          parentDefs={selfDefs}
          parent={record}
        />
        {optional && !readOnly ? (
          <button type="button" className="btn-link text-xs -mt-2 mb-2 block" onClick={() => clear(childPath)}>
            Remove {labelFor(key, deref(property)?.title)}
          </button>
        ) : null}
      </div>
    );
  });

  // The panel root has no label of its own, and neither does an array item —
  // its numbered subgroup is the label.
  if (path.length === 0 || hideLabel) {
    return (
      <>
        {unmet ? <p className="fb-field-error">Fill in one of: {alternatives.map((g) => g.join(" + ")).join(" or ")}.</p> : null}
        {body}
      </>
    );
  }

  return (
    <div className="fb-field">
      <span className="fb-field-label">{labelFor(propertyName, schema.title)}</span>
      {required ? null : <span className="fb-empty"> optional</span>}
      <div className="fb-subgroup">
        {unmet ? <p className="fb-field-error">Fill in one of: {alternatives.map((g) => g.join(" or ")).join(", ")}.</p> : null}
        {body}
      </div>
    </div>
  );
}

/**
 * A list, with the schema's own `minItems`/`maxItems` as the add/remove gates
 * and move controls so order is editable — which matters for `blocks` (send
 * order), `buttons` (display order) and `actions` (execution order).
 */
export function ArrayField({ schema, path, value, propertyName }: FieldProps) {
  const { set, readOnly } = useField();
  const items = Array.isArray(value) ? value : [];
  // minItems is the real gate on removal — `randomizer.paths` needs two, and
  // dropping below that is a document-stage error, not a warning.
  const min = schema.minItems ?? 0;
  const max = schema.maxItems;

  const move = (from: number, to: number) => {
    if (to < 0 || to >= items.length) {
      return;
    }
    const next = [...items];
    const [moved] = next.splice(from, 1);
    next.splice(to, 0, moved);
    set(path, next, `move:${formatPath(path)}`);
  };

  return (
    <div className="fb-field">
      <span className="fb-field-label">{labelFor(propertyName, schema.title)}</span>
      {max !== undefined ? <span className="fb-empty"> {items.length}/{max}</span> : null}

      {items.length === 0 ? <p className="fb-empty">Nothing yet.</p> : null}

      {items.map((item, index) => (
        <div key={index} className="fb-subgroup">
          <div className="flex items-center gap-1 mb-1">
            <span className="fb-empty">#{index + 1}</span>
            {readOnly ? null : (
              <span className="ml-auto flex gap-1">
                <button
                  type="button"
                  className="btn-link text-xs"
                  onClick={() => move(index, index - 1)}
                  aria-label={`Move ${labelFor(propertyName, schema.title)} ${index + 1} up`}
                >
                  ↑
                </button>
                <button
                  type="button"
                  className="btn-link text-xs"
                  onClick={() => move(index, index + 1)}
                  aria-label={`Move ${labelFor(propertyName, schema.title)} ${index + 1} down`}
                >
                  ↓
                </button>
                <button
                  type="button"
                  className="btn-link text-xs"
                  disabled={items.length <= min}
                  onClick={() => set(path, items.filter((_unused, at) => at !== index), `remove:${formatPath(path)}`)}
                  aria-label={`Remove ${labelFor(propertyName, schema.title)} ${index + 1}`}
                >
                  Remove
                </button>
              </span>
            )}
          </div>
          <SchemaField
            schema={schema.items}
            path={[...path, index]}
            value={item}
            propertyName={propertyName}
            required
            hideLabel
          />
        </div>
      ))}

      {readOnly || (max !== undefined && items.length >= max) ? null : (
        <button
          type="button"
          className="btn-outline-sm mt-1"
          // Named after the list: a panel can hold several arrays, and three
          // buttons all called "Add" say nothing about what they add.
          aria-label={`Add to ${labelFor(propertyName, schema.title)}`}
          onClick={() => set(path, [...items, defaultFor(schema.items, propertyName)], `append:${formatPath(path)}`)}
        >
          Add
        </button>
      )}
    </div>
  );
}

/**
 * A tagged union.
 *
 * Choices come from `discriminator.mapping` keys, never from the `oneOf`
 * branches: `message_block` has seven tags over four branches, so enumerating
 * branches would silently lose image, audio, video and file.
 *
 * Switching a tag **replaces** the value rather than merging into it. Merging
 * leaves the previous branch's keys behind, every object here is closed, and
 * the result is `unknown_config_key` — a 422 that discards the whole save.
 */
export function VariantField({
  schema,
  path,
  value,
  propertyName,
  required,
  rawSchema,
  hideLabel = false,
}: FieldProps & { rawSchema: JsonSchema; hideLabel?: boolean }) {
  const field = useField();
  const { set, readOnly } = field;
  const property = rawSchema.discriminator?.propertyName ?? "type";
  const tags = variantChoices(rawSchema);
  const current =
    typeof value === "object" && value !== null ? (value as Record<string, unknown>)[property] : undefined;
  const branch = variantFor(rawSchema, value);
  const branchDefs =
    typeof current === "string" ? defsName({ $ref: rawSchema.discriminator?.mapping[current] ?? "" }) : undefined;

  // A branch can have a widget of its own — `$block_media` is one object shape
  // reached through four of message_block's seven tags. The tag selector above
  // stays the only thing that switches branches, so there is never a second
  // control writing the discriminator.
  const BranchOverride = branchDefs
    ? lookupOverride({ nodeType: field.nodeType, path, selfDefs: branchDefs, parentDefs: undefined, propertyName })
    : undefined;

  return (
    <div className="fb-field">
      {/* Inside an array item the numbered subgroup is already the heading, so
          a visible label here would be the second of three identical ones. The
          accessible name is kept either way. */}
      {hideLabel ? null : (
        <>
          <label className="fb-field-label" htmlFor={fieldId(path)}>
            {labelFor(propertyName, schema.title)}
          </label>
          {required ? null : <span className="fb-empty"> optional</span>}
        </>
      )}
      <select
        id={fieldId(path)}
        aria-label={labelFor(propertyName, schema.title)}
        className="bb-select"
        value={typeof current === "string" ? current : ""}
        disabled={readOnly}
        onChange={(event) => set(path, seedVariant(rawSchema, event.target.value), `variant:${formatPath(path)}`)}
      >
        <option value="">Choose…</option>
        {tags.map((tag) => (
          <option key={tag} value={tag}>
            {variantLabel(tag)}
          </option>
        ))}
      </select>
      {branch ? (
        <div className="fb-subgroup">
          {BranchOverride ? (
            <BranchOverride schema={branch} path={path} value={value} propertyName={propertyName} required={required} />
          ) : (
            <ObjectField
              schema={branch}
              path={path}
              value={value}
              propertyName={propertyName}
              required={required}
              selfDefs={branchDefs}
              // The variant selector immediately above IS this group's label.
              hideLabel
            />
          )}
        </div>
      ) : null}
    </div>
  );
}

/**
 * A `oneOf` with no discriminator: the reader picks a branch by name.
 *
 * Contract 8's condition rules are this shape. Each branch pins `source` to a
 * literal and narrows `op` to the operators that source supports, so choosing
 * "Tag" here leaves only `has` / `has_not` in the operator list — without this
 * bundle carrying a copy of that mapping. Two sources contribute two branches
 * each, which is why the chooser is labelled by the branch `title` rather than
 * by the source.
 *
 * Switching replaces the value, for the same reason a tagged union does: every
 * object is closed, so a leftover key from the previous branch is
 * `unknown_config_key`.
 */
export function UnionField({
  schema,
  path,
  value,
  propertyName,
  required,
  rawSchema,
  hideLabel = false,
}: FieldProps & { rawSchema: JsonSchema; hideLabel?: boolean }) {
  const { set, readOnly } = useField();
  const labels = branchLabels(rawSchema);
  const current = matchBranch(rawSchema, value);
  const branch = current === -1 ? undefined : branchAt(rawSchema, current);

  // A union of scalars — a rule's `value` is string | number | boolean |
  // {relative}. The chooser names the kinds; the branch renders the input.
  const scalarBranch = branch && !branch.properties && typesOf(branch).length > 0;

  return (
    <div className="fb-field">
      {hideLabel ? null : (
        <>
          <label className="fb-field-label" htmlFor={fieldId(path)}>
            {labelFor(propertyName, schema.title)}
          </label>
          {required ? null : <span className="fb-empty"> optional</span>}
        </>
      )}
      <select
        id={fieldId(path)}
        aria-label={labelFor(propertyName, schema.title)}
        className="bb-select"
        value={current === -1 ? "" : String(current)}
        disabled={readOnly}
        onChange={(event) =>
          set(path, defaultFor(branchAt(rawSchema, Number(event.target.value))), `union:${formatPath(path)}`)
        }
      >
        {current === -1 ? <option value="">Choose…</option> : null}
        {labels.map((label, index) => (
          <option key={index} value={index}>
            {label}
          </option>
        ))}
      </select>
      {branch ? (
        <div className="fb-subgroup">
          {scalarBranch ? (
            <SchemaField schema={branch} path={path} value={value} propertyName={propertyName} required hideLabel />
          ) : (
            <ObjectField
              schema={branch}
              path={path}
              value={value}
              propertyName={propertyName}
              required={required}
              hideLabel
            />
          )}
        </div>
      ) : null}
    </div>
  );
}

/** Whether a random item id would satisfy this property's own grammar. */
function mintableId(property: JsonSchema | undefined): boolean {
  const pattern = deref(property)?.pattern;
  return pattern === undefined || pattern === ID_PATTERN.source;
}

/** A fresh value for one union branch — the tag plus its required fields. */
function seedVariant(schema: JsonSchema, tag: string): unknown {
  const property = schema.discriminator?.propertyName ?? "type";
  const branchRef = schema.discriminator?.mapping[tag];
  const branch = branchRef ? deref({ $ref: branchRef }) : undefined;
  const seeded = defaultFor(branch);
  return { ...(typeof seeded === "object" && seeded !== null ? seeded : {}), [property]: tag };
}

/**
 * The value a newly added property or list item starts as.
 *
 * Derived from the schema so a later node type's fields work, and required
 * scalars start as a value the schema accepts rather than `""` — most strings
 * here carry `minLength: 1`, so an empty seed is a validation error the user
 * did not cause.
 */
export function defaultFor(schema: JsonSchema | undefined, propertyName = ""): unknown {
  const resolved = deref(schema);
  if (!resolved) {
    return undefined;
  }

  const pinned = constValue(resolved);
  if (pinned !== undefined) {
    return pinned;
  }
  if (isTaggedUnion(resolved)) {
    const first = variantChoices(resolved)[0];
    return first === undefined ? {} : seedVariant(resolved, first);
  }
  if (isUntaggedUnion(resolved)) {
    return defaultFor(branchAt(resolved, 0), propertyName);
  }
  if (resolved.enum && resolved.enum.length > 0) {
    return resolved.enum[0];
  }

  const types = typesOf(resolved);
  if (types.includes("array")) {
    return Array.from({ length: resolved.minItems ?? 0 }, () => defaultFor(resolved.items, propertyName));
  }
  if (types.includes("object") || resolved.properties) {
    const out: Record<string, unknown> = {};
    const required = new Set(resolved.required ?? []);
    // Same rule as schema/sample.ts: satisfy the anyOf, and prefer the branch
    // that is not an id reference.
    for (const key of preferredBranch(anyOfRequirements(resolved))) {
      required.add(key);
    }
    for (const [key, property] of Object.entries(resolved.properties ?? {})) {
      if (required.has(key) || constValue(deref(property)) !== undefined) {
        // An `id` backs a dynamic handle, so it has to be unique per item —
        // a shared placeholder would collide two `btn:` handles. Only mint one
        // where the schema's own grammar allows it: a node type added later
        // with a UUID-shaped `id` needs a UUID, and a token that fails its
        // pattern is a document-stage error that discards the whole save.
        const seeded = key === "id" && mintableId(property) ? newItemId() : defaultFor(property, key);
        if (seeded !== undefined) {
          out[key] = seeded;
        }
      }
    }
    return out;
  }
  if (types.includes("boolean")) {
    return false;
  }
  if (types.includes("integer") || types.includes("number")) {
    return resolved.minimum ?? 0;
  }
  // Never "" for a string the schema would reject — and readable copy rather
  // than an ellipsis, so a button added in the panel says "Button".
  //
  // `pattern` counts as much as `minLength` here: a condition rule's `key` is a
  // UUID with no minimum length, so an empty seed passes the length check and
  // fails the pattern — a document-stage error, which discards the *entire*
  // save rather than just the rule the reader has not filled in yet.
  return (resolved.minLength ?? 0) > 0 || resolved.pattern
    ? placeholderFor(propertyName, resolved)
    : "";
}
