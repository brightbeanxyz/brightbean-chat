/**
 * SPEC §11.4's filter: a match mode and a list of rules.
 *
 * The operator list shown for a source is narrowed for usability, but the
 * **schema's** twenty-two-value enum stays the authority — this is a hint, not
 * a second operator table. ROADMAP contract 8 puts the real one in
 * apps.contacts.conditions, and duplicating it here is exactly the drift the
 * brief warns about.
 */
import { deref } from "../../schema/resolve";
import type { JsonSchema } from "../../schema/types";
import { formatPath } from "../../store/paths";
import { useField } from "../FieldContext";
import { SchemaField } from "../SchemaField";
import type { FieldProps } from "../fields";

/** Which operators are worth offering first, per source. Advisory only. */
const SUGGESTED: Record<string, string[]> = {
  tag: ["has", "has_not"],
  sequence: ["subscribed", "not_subscribed"],
  window: ["inside", "outside"],
  custom_field: ["is", "is_not", "contains", "has_value", "no_value", "=", "!=", ">", "<", ">=", "<="],
  system_field: ["is", "is_not", "contains", "has_value", "no_value"],
  segment: ["is", "is_not"],
};

/** Operators that compare against nothing, so the value box is hidden. */
const UNARY = new Set(["has_value", "no_value", "has", "has_not", "subscribed", "not_subscribed", "inside", "outside"]);

function ruleSchemas(schema: JsonSchema) {
  const rules = deref(schema.properties?.["rules"]);
  const rule = deref(rules?.items);
  return {
    source: rule?.properties?.["source"],
    op: rule?.properties?.["op"],
    key: rule?.properties?.["key"],
    value: rule?.properties?.["value"],
    rules,
    rule,
  };
}

export function ConditionRuleBuilder(props: FieldProps) {
  const { set, readOnly } = useField();
  const { schema, path, value } = props;
  const config = (typeof value === "object" && value !== null ? value : {}) as Record<string, unknown>;
  const rules = Array.isArray(config["rules"]) ? (config["rules"] as Record<string, unknown>[]) : [];
  const parts = ruleSchemas(schema);
  const max = parts.rules?.maxItems;

  const allOps = (parts.op && deref(parts.op)?.enum) ?? [];
  const opsFor = (source: unknown) => {
    const suggested = SUGGESTED[String(source)];
    return suggested ? suggested.filter((op) => allOps.includes(op)) : allOps.map(String);
  };

  const writeRules = (next: Record<string, unknown>[]) =>
    set([...path, "rules"], next, `rules:${formatPath(path)}`);

  return (
    <>
      <div className="fb-field">
        <span className="fb-field-label">Match</span>
        <select
          className="bb-select"
          value={String(config["match"] ?? "all")}
          disabled={readOnly}
          onChange={(event) => set([...path, "match"], event.target.value, `match:${formatPath(path)}`)}
        >
          <option value="all">All of the rules</option>
          <option value="any">Any of the rules</option>
        </select>
      </div>

      <div className="fb-field">
        <span className="fb-field-label">
          Rules{max !== undefined ? <span className="fb-empty"> {rules.length}/{max}</span> : null}
        </span>
        {rules.length === 0 ? <p className="fb-empty">No rules yet — this condition always takes the No branch.</p> : null}

        {rules.map((rule, index) => (
          <div key={index} className="fb-subgroup">
            <div className="flex gap-1 mb-1">
              <select
                className="bb-select"
                aria-label="Source"
                value={String(rule["source"] ?? "")}
                disabled={readOnly}
                onChange={(event) => {
                  const source = event.target.value;
                  const ops = opsFor(source);
                  // Changing the source can strand an operator the new source
                  // does not use, so pick a valid one rather than leaving the
                  // rule in a state the server will reject.
                  const op = ops.includes(String(rule["op"])) ? rule["op"] : ops[0];
                  writeRules(rules.map((entry, at) => (at === index ? { ...entry, source, op } : entry)));
                }}
              >
                <option value="">Source…</option>
                {((parts.source && deref(parts.source)?.enum) ?? []).map((source) => (
                  <option key={String(source)} value={String(source)}>
                    {String(source).replace(/_/g, " ")}
                  </option>
                ))}
              </select>

              <select
                className="bb-select"
                aria-label="Operator"
                value={String(rule["op"] ?? "")}
                disabled={readOnly}
                onChange={(event) =>
                  writeRules(rules.map((entry, at) => (at === index ? { ...entry, op: event.target.value } : entry)))
                }
              >
                <option value="">Operator…</option>
                {opsFor(rule["source"]).map((op) => (
                  <option key={String(op)} value={String(op)}>
                    {String(op)}
                  </option>
                ))}
              </select>

              {readOnly ? null : (
                <button
                  type="button"
                  className="btn-link text-xs"
                  aria-label="Remove rule"
                  onClick={() => writeRules(rules.filter((_unused, at) => at !== index))}
                >
                  Remove
                </button>
              )}
            </div>

            <SchemaField
              schema={parts.key}
              path={[...path, "rules", index, "key"]}
              value={rule["key"]}
              propertyName="key"
            />
            {UNARY.has(String(rule["op"])) ? null : (
              <SchemaField
                schema={parts.value}
                path={[...path, "rules", index, "value"]}
                value={rule["value"]}
                propertyName="value"
              />
            )}
          </div>
        ))}

        {readOnly || (max !== undefined && rules.length >= max) ? null : (
          <button
            type="button"
            className="btn-outline-sm mt-1"
            onClick={() => writeRules([...rules, { source: "tag", op: "has", key: "" }])}
          >
            Add a rule
          </button>
        )}
      </div>
    </>
  );
}
