/**
 * A text field that knows about `{{placeholder}}` tokens.
 *
 * Typing `{{` opens a list of what can be substituted. The tokens are inserted
 * as literal text and nothing here evaluates anything — SECURITY-BASELINE §3
 * bans template-engine evaluation of user content, and L3-B owns the only
 * renderer, at apps/flows/rendering.py. `SYSTEM_TOKENS` below is the single
 * place to reconcile with it when it lands.
 */
import { useRef, useState } from "react";

import { FieldShell, fieldId, type FieldProps } from "../fields";
import { useField } from "../FieldContext";

/** SPEC §9.2's system fields. Custom fields and variables come from the API. */
export const SYSTEM_TOKENS = ["first_name", "last_name", "email", "phone"];

export function PlaceholderInput(props: FieldProps) {
  const { set, readOnly, picklists } = useField();
  const { schema, path, value } = props;
  const [open, setOpen] = useState(false);
  const input = useRef<HTMLTextAreaElement | HTMLInputElement>(null);

  const text = typeof value === "string" ? value : "";
  const long = (schema.maxLength ?? 0) > 512;
  const tokens = [...SYSTEM_TOKENS, ...picklists.custom_fields.map((field) => field.id)];

  const insert = (token: string) => {
    const element = input.current;
    const at = element?.selectionStart ?? text.length;
    // Replace the `{{` that opened the list, so the result is one clean token.
    const before = text.slice(0, at).replace(/\{\{$/, "");
    set(path, `${before}{{${token}}}${text.slice(at)}`, `text:${path.join(".")}`);
    setOpen(false);
  };

  const onChange = (next: string, caret: number) => {
    set(path, next, `text:${path.join(".")}`);
    setOpen(next.slice(Math.max(0, caret - 2), caret) === "{{");
  };

  const common = {
    id: fieldId(path),
    className: "form-input-styled",
    value: text,
    disabled: readOnly,
    maxLength: schema.maxLength,
  };

  return (
    <FieldShell {...props}>
      {long ? (
        <textarea
          {...common}
          rows={5}
          ref={input as React.RefObject<HTMLTextAreaElement>}
          onChange={(event) => onChange(event.target.value, event.target.selectionStart ?? 0)}
        />
      ) : (
        <input
          {...common}
          type="text"
          ref={input as React.RefObject<HTMLInputElement>}
          onChange={(event) => onChange(event.target.value, event.target.selectionStart ?? 0)}
        />
      )}
      {open && !readOnly ? (
        <div className="fb-subgroup" role="listbox" aria-label="Insert a placeholder">
          {tokens.map((token) => (
            <button key={token} type="button" role="option" className="fb-palette-item" onClick={() => insert(token)}>
              {`{{${token}}}`}
            </button>
          ))}
        </div>
      ) : (
        <p className="fb-field-help">
          Type <code>{"{{"}</code> to insert a contact field.
        </p>
      )}
    </FieldShell>
  );
}
