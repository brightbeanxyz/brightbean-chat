/**
 * The primitive widgets the dispatcher falls back to.
 *
 * Each is driven entirely by its schema — `minLength`, `maxLength`, `minimum`,
 * `maximum`, `enum`, `pattern` — so a property added to a node type in a later
 * layer gets correct constraints with no code here.
 */
import type { ReactNode } from "react";

import type { JsonSchema } from "../schema/types";
import type { ConfigPath } from "../store/paths";
import { formatPath } from "../store/paths";
import { issuesForField } from "../validation/normalize";
import { useField } from "./FieldContext";
import { labelFor } from "./copy";

export interface FieldProps {
  /** Already dereferenced. */
  schema: JsonSchema;
  path: ConfigPath;
  value: unknown;
  propertyName: string;
  required: boolean;
}

/**
 * A stable DOM id for one field, derived from its path.
 *
 * Derived rather than useId() so the label association is deterministic — a
 * generated form is only usable with a screen reader, or testable by label, if
 * every control is actually associated with its text.
 */
export function fieldId(path: ConfigPath): string {
  return `fb-${path.map(String).join("-") || "root"}`;
}

/** Label, control, help text, and any server message about this exact field. */
export function FieldShell({
  schema,
  path,
  propertyName,
  required,
  children,
  onClear,
}: FieldProps & { children: ReactNode; onClear?: () => void }) {
  const { issues, readOnly } = useField();
  const messages = issuesForField(issues, formatPath(path));

  return (
    <div className="fb-field">
      {/* The "optional" marker and the Clear button sit OUTSIDE the <label>.
          Inside it they become part of the control's accessible name, so a
          screen reader announces "Note optional" as the field's name — and
          every label-based query has to know about the suffix. */}
      <div className="flex items-center gap-1">
        <label className="fb-field-label" htmlFor={fieldId(path)}>
          {labelFor(propertyName, schema.title)}
        </label>
        {required ? null : <span className="fb-empty">optional</span>}
        {onClear && !required && !readOnly ? (
          <button type="button" className="btn-link ml-auto text-xs" onClick={onClear}>
            Clear
          </button>
        ) : null}
      </div>
      {children}
      {schema.description ? <p className="fb-field-help">{schema.description}</p> : null}
      {messages.map((issue, index) => (
        <p key={index} className={issue.severity === "error" ? "fb-field-error" : "fb-field-help"}>
          {issue.message}
        </p>
      ))}
    </div>
  );
}

export function TextField(props: FieldProps) {
  const { set, readOnly } = useField();
  const { schema, path, value } = props;
  const long = (schema.maxLength ?? 0) > 512;

  const common = {
    id: fieldId(path),
    className: "form-input-styled",
    value: typeof value === "string" ? value : "",
    disabled: readOnly,
    maxLength: schema.maxLength,
    onChange: (event: { target: { value: string } }) =>
      set(path, event.target.value, `text:${path.join(".")}`),
  };

  return (
    <FieldShell {...props}>
      {long ? <textarea {...common} rows={5} /> : <input type="text" {...common} />}
    </FieldShell>
  );
}

export function NumberField(props: FieldProps) {
  const { set, readOnly } = useField();
  const { schema, path, value } = props;

  return (
    <FieldShell {...props}>
      <input
        id={fieldId(path)}
        type="number"
        className="form-input-styled"
        value={typeof value === "number" ? value : ""}
        disabled={readOnly}
        min={schema.minimum}
        max={schema.maximum}
        onChange={(event) => {
          const parsed = Number(event.target.value);
          // An empty box is "no value", not zero: writing 0 into a field whose
          // minimum is 1 turns a blank into a validation error.
          set(path, event.target.value === "" || Number.isNaN(parsed) ? undefined : parsed, `num:${path.join(".")}`);
        }}
      />
    </FieldShell>
  );
}

export function ToggleField(props: FieldProps) {
  const { set, readOnly } = useField();
  const { path, value, schema, propertyName } = props;

  return (
    <div className="fb-field">
      <label className="flex items-center gap-2 text-xs" style={{ color: "var(--text-primary)" }}>
        <input
          id={fieldId(path)}
          type="checkbox"
          className="bb-checkbox"
          checked={value === true}
          disabled={readOnly}
          onChange={(event) => set(path, event.target.checked, `bool:${path.join(".")}`)}
        />
        <span>{labelFor(propertyName, schema.title)}</span>
      </label>
      {schema.description ? <p className="fb-field-help">{schema.description}</p> : null}
    </div>
  );
}

export function SelectField(props: FieldProps) {
  const { set, readOnly } = useField();
  const { schema, path, value, required } = props;
  const options = (schema.enum ?? []).map(String);

  return (
    <FieldShell {...props}>
      <select
        id={fieldId(path)}
        className="bb-select"
        value={typeof value === "string" ? value : ""}
        disabled={readOnly}
        onChange={(event) => set(path, event.target.value, `enum:${path.join(".")}`)}
      >
        {required && typeof value === "string" && options.includes(value) ? null : <option value="">Choose…</option>}
        {options.map((option) => (
          <option key={option} value={option}>
            {option}
          </option>
        ))}
      </select>
    </FieldShell>
  );
}

/**
 * A value the schema types as `["string", "number", "boolean", "null"]` — which
 * in this schema means a condition rule's comparison value. The kind matters:
 * the server distinguishes `"5"` from `5`.
 */
export function ScalarField(props: FieldProps) {
  const { set, readOnly } = useField();
  const { path, value } = props;
  const kind = typeof value === "number" ? "number" : typeof value === "boolean" ? "boolean" : "text";

  return (
    <FieldShell {...props}>
      <div className="flex gap-1">
        <select
          id={fieldId(path)}
          className="bb-select w-28"
          aria-label="Value kind"
          value={kind}
          disabled={readOnly}
          onChange={(event) => {
            const next = event.target.value;
            set(path, next === "number" ? 0 : next === "boolean" ? true : String(value ?? ""), `scalar:${path.join(".")}`);
          }}
        >
          <option value="text">Text</option>
          <option value="number">Number</option>
          <option value="boolean">Yes / no</option>
        </select>
        {kind === "boolean" ? (
          <select
            className="bb-select"
            value={value === true ? "true" : "false"}
            disabled={readOnly}
            onChange={(event) => set(path, event.target.value === "true", `scalar:${path.join(".")}`)}
          >
            <option value="true">Yes</option>
            <option value="false">No</option>
          </select>
        ) : (
          <input
            type={kind === "number" ? "number" : "text"}
            className="form-input-styled"
            value={value === undefined || value === null ? "" : String(value)}
            disabled={readOnly}
            onChange={(event) =>
              set(
                path,
                kind === "number" ? Number(event.target.value) : event.target.value,
                `scalar:${path.join(".")}`,
              )
            }
          />
        )}
      </div>
    </FieldShell>
  );
}

/**
 * Arbitrary JSON — `external_request.body`, which the schema types as `{}`.
 *
 * Parsed on blur, keeping the last valid value in the config when the text does
 * not parse. Writing `undefined` mid-typing would drop the key and lose work.
 */
export function JsonField(props: FieldProps) {
  const { set, readOnly } = useField();
  const { path, value } = props;

  return (
    <FieldShell {...props}>
      <textarea
        id={fieldId(path)}
        className="form-input-styled font-mono text-xs"
        rows={6}
        disabled={readOnly}
        defaultValue={value === undefined ? "" : JSON.stringify(value, null, 2)}
        onBlur={(event) => {
          const text = event.target.value.trim();
          if (text === "") {
            set(path, undefined, `json:${path.join(".")}`);
            event.target.setCustomValidity("");
            return;
          }
          try {
            set(path, JSON.parse(text), `json:${path.join(".")}`);
            event.target.setCustomValidity("");
          } catch (error) {
            event.target.setCustomValidity(error instanceof Error ? error.message : "Invalid JSON");
            event.target.reportValidity();
          }
        }}
      />
    </FieldShell>
  );
}
