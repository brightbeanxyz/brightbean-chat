/**
 * The `key` of a condition rule, which means a different thing per source.
 *
 * Contract 8 addresses tags, custom fields, segments and sequences by their
 * primary keys — UUIDs — so a plain text box would ask the reader to paste one.
 * Which pick-list applies is decided by the `source` sitting beside this field,
 * which is why the widget reads its parent.
 *
 * There is deliberately no source-to-operator table here. The schema encodes
 * that: each rule branch pins `source` and narrows `op` to what that source
 * supports, so the generic union renderer already offers only the valid
 * operators. A copy of apps/contacts/conditions.py's mapping in this bundle is
 * exactly the drift the brief warns about.
 */
import type { Picklists } from "../../schema/types";
import { FieldShell, TextField, fieldId, type FieldProps } from "../fields";
import { useField } from "../FieldContext";

/** Which pick-list a source's `key` is drawn from, when it is drawn from one. */
const LIST_FOR_SOURCE: Record<string, keyof Picklists> = {
  tag: "tags",
  custom_field: "custom_fields",
  sequence: "sequences",
};

export function ConditionKeySelect(props: FieldProps) {
  const { set, readOnly, picklists } = useField();
  const { path, value, parent } = props;

  const source = typeof parent === "object" && parent !== null ? (parent as Record<string, unknown>)["source"] : undefined;
  const list = LIST_FOR_SOURCE[String(source)];

  // `system_field`, `segment` and `window` have no pick-list here: system field
  // names are a fixed vocabulary the schema already constrains, segments are
  // #3's and not exposed yet, and a window key is a platform. Plain text keeps
  // those honest rather than offering an empty dropdown.
  if (!list) {
    return <TextField {...props} />;
  }

  const options = picklists[list];
  const current = typeof value === "string" ? value : "";
  const known = options.some((option) => option.id === current);

  return (
    <FieldShell {...props}>
      <select
        id={fieldId(path)}
        className="bb-select"
        value={current}
        disabled={readOnly}
        onChange={(event) => set(path, event.target.value, `key:${path.join(".")}`)}
      >
        <option value="">Choose…</option>
        {options.map((option) => (
          <option key={option.id} value={option.id}>
            {option.label}
          </option>
        ))}
        {current && !known ? <option value={current}>{current} (no longer available)</option> : null}
      </select>
      {options.length === 0 ? (
        <p className="fb-field-help">
          Nothing to choose from yet — create one first, then come back to this rule.
        </p>
      ) : null}
    </FieldShell>
  );
}
