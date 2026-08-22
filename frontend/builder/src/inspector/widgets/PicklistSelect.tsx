/**
 * A select backed by one of apps/flows/picklists.py's six lists.
 *
 * Tags, custom fields and sequences are empty until issue #3 and L6-A land, and
 * apps/flows/picklists.py says a client "that has to branch on which keys are
 * present is a client that will break on the day one of them starts arriving".
 * So the ones whose values are free text stay typeable: a dead dropdown would
 * make send_message unusable on a fresh install, and the value is a name the
 * server accepts either way.
 *
 * Flows and members are different — those are ids, and inventing one produces a
 * reference to nothing. Those stay closed lists.
 */
import type { Picklists } from "../../schema/types";
import { FieldShell, fieldId, type FieldProps } from "../fields";
import { useField } from "../FieldContext";

type ListKey = keyof Picklists;

const EMPTY_COPY: Partial<Record<ListKey, string>> = {
  tags: "Tags arrive with the contacts app — type a name to use it now.",
  custom_fields: "Custom fields arrive with the contacts app — type a name to use it now.",
  sequences: "Sequences arrive with the sequences app — type a name to use it now.",
  flows: "This workspace has no other flows yet.",
  members: "No workspace members to choose from.",
  connections: "No channels are connected yet.",
};

export function picklistSelect(list: ListKey, { creatable }: { creatable: boolean }) {
  return function PicklistSelect(props: FieldProps) {
    const { set, readOnly, picklists } = useField();
    const { path, value } = props;
    const options = picklists[list];
    const current = typeof value === "string" ? value : "";
    const known = options.some((option) => option.id === current);
    const listId = `fb-list-${list}-${path.join("-")}`;

    if (creatable) {
      return (
        <FieldShell {...props}>
          <input
            id={fieldId(path)}
            type="text"
            className="form-input-styled"
            list={listId}
            value={current}
            disabled={readOnly}
            onChange={(event) => set(path, event.target.value, `pick:${path.join(".")}`)}
          />
          <datalist id={listId}>
            {options.map((option) => (
              <option key={option.id} value={option.id}>
                {option.label}
              </option>
            ))}
          </datalist>
          {options.length === 0 ? <p className="fb-field-help">{EMPTY_COPY[list]}</p> : null}
        </FieldShell>
      );
    }

    return (
      <FieldShell {...props}>
        <select
          id={fieldId(path)}
          className="bb-select"
          value={current}
          disabled={readOnly}
          onChange={(event) => set(path, event.target.value, `pick:${path.join(".")}`)}
        >
          <option value="">Choose…</option>
          {options.map((option) => (
            <option key={option.id} value={option.id}>
              {option.label}
            </option>
          ))}
          {/* A value the list no longer offers — an archived flow, a member who
              left. Kept selectable so opening the panel does not silently drop
              it from the config. */}
          {current && !known ? <option value={current}>{current} (no longer available)</option> : null}
        </select>
        {options.length === 0 ? <p className="fb-field-help">{EMPTY_COPY[list]}</p> : null}
      </FieldShell>
    );
  };
}

/** `notify_members.member_ids` — a list of user ids, so a closed multi-select. */
export function MemberMultiSelect(props: FieldProps) {
  const { set, readOnly, picklists } = useField();
  const { path, value } = props;
  const selected = Array.isArray(value) ? value.map(String) : [];

  const toggle = (id: string) =>
    set(
      path,
      selected.includes(id) ? selected.filter((entry) => entry !== id) : [...selected, id],
      `members:${path.join(".")}`,
    );

  return (
    <FieldShell {...props}>
      {picklists.members.length === 0 ? <p className="fb-field-help">No workspace members to choose from.</p> : null}
      {picklists.members.map((member) => (
        <label key={member.id} className="flex items-center gap-2 text-xs mb-1">
          <input
            type="checkbox"
            className="bb-checkbox"
            checked={selected.includes(member.id)}
            disabled={readOnly}
            onChange={() => toggle(member.id)}
          />
          <span>{member.label}</span>
        </label>
      ))}
    </FieldShell>
  );
}
