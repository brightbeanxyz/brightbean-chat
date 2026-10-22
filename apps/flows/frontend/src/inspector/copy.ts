/**
 * Human labels for schema properties.
 *
 * The artefact carries almost no `title` keywords, and its `description`s are
 * developer copy citing SPEC sections — useful as help text, wrong as a label.
 * So a property falls back through: schema `title`, a translated lookup, then
 * a humanised property name. A node type added later gets the humanised form,
 * which is serviceable; adding a translation key here is the optional polish.
 *
 * Every lookup is resolved through `i18n.t()` at call time, not stored as a
 * frozen table — these functions are called fresh on every render, so the
 * active language is always the one that renders.
 */
import i18n from "../i18n";

function lookup(namespace: string, key: string): string | undefined {
  const path = `inspector.${namespace}.${key}`;
  const rendered = i18n.t(path);
  return rendered === path ? undefined : rendered;
}

/** `html_body` -> `Html body`, as a last resort. */
export function humanize(name: string): string {
  const words = name.replace(/[_-]+/g, " ").trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

export function labelFor(name: string, title?: string): string {
  return title ?? lookup("labels", name) ?? humanize(name);
}

/**
 * Help text below a field, translated.
 *
 * Unlike `labelFor`, this checks the schema's own `description` *first*: most
 * fields carry none, and a property name shared across node types (`field`,
 * `value`) may have a description on one and none on another. Looking the
 * translation up before checking for a description would show help text on a
 * field the schema never gave one to. A description with no translated entry
 * falls back to the artefact's own English copy — the same "still readable"
 * fallback `labelFor` uses — rather than showing nothing.
 */
export function helpFor(name: string, description?: string): string | undefined {
  if (!description) return undefined;
  return lookup("help", name) ?? description;
}

/**
 * Tag copy for the discriminated unions a person actually picks from, falling
 * through to the enum labels below (several keys are shared, e.g. `date`),
 * and finally to `humanize`.
 *
 * Deliberately two separate namespaces rather than one merged table: a tagged
 * union's tag names a *shape* a reader picks ("Wait a fixed time"), and an
 * enum value names a value inside a field. Several keys collide with
 * different right answers — `date` is "Wait until a date" as a delay's mode
 * and "A date" as an expected reply.
 */
export function variantLabel(tag: string): string {
  return lookup("variantLabels", tag) ?? lookup("enumLabels", tag) ?? humanize(tag);
}

/**
 * What one item of a list is called, for the button that appends one.
 *
 * The adder used to read "Add", with "Add to Buttons" as its accessible name —
 * so a screen reader was told what it added and a reader was not. This is the
 * visible half, and it names the *thing*, not the list: you add a button, not
 * a Buttons.
 *
 * A list with no entry here falls back to "Add to <list label>" (built by the
 * caller), which is serviceable and still says what it adds.
 */
export function addOneLabel(propertyName: string): string | undefined {
  return lookup("addOne", propertyName);
}

/**
 * How the "Add to this step" chips are grouped, and in what order.
 *
 * The headings answer "what is this for?" without opening anything, which a
 * flat row of four cannot: two of a send_message step's options are about
 * somebody going quiet and one is WhatsApp-only, and nothing said so.
 *
 * Keys, not node types, because the same property means the same thing
 * wherever it appears — `followup` is a wait on send_message and on
 * data_collection alike. A property in no group falls into the last one, so a
 * property added by a later layer is never silently dropped off the panel.
 *
 * `key` is the group's stable identity — used for matching and as a React
 * list key — and stays the same across languages; `addGroupLabel(key)` is
 * the translated heading, resolved at render time so it is never frozen to
 * whatever language was active when this module loaded.
 */
export const ADD_GROUPS: readonly { key: string; keys: readonly string[] }[] = [
  { key: "tappable", keys: ["buttons", "quick_replies"] },
  { key: "ifTheyGoQuiet", keys: ["followup", "retry_unmatched", "retry", "timeout"] },
  { key: "whatsappOnly", keys: ["whatsapp_template", "template"] },
  { key: "more", keys: [] },
];

export function addGroupLabel(key: string): string {
  return i18n.t(`inspector.addGroups.${key}`);
}
