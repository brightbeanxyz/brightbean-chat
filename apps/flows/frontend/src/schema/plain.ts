/**
 * Reader-facing names for node types.
 *
 * The registry's own `label` is developer copy — it names the mechanism
 * ("Send Message", "Smart Delay") and its `description` quotes SPEC sections.
 * That is right for the registry, which is read by the API, the exporter and
 * whoever is adding a node type. It is the wrong register for a canvas somebody
 * is reading to work out what their flow does — and, unlike the eyebrow below,
 * it is also the wrong *language* the moment the reader is not English: the
 * registry is never translated (see `nodeTypeLabel`'s own doc for why), so
 * anywhere that text was the reader's only copy read English regardless of
 * locale until `nodeTypeLabel`/`nodeTypeDescription` gave it a translated one.
 *
 * So a card now carries two lines: an eyebrow saying what KIND of step this is
 * in the reader's words ("Then send", "Then decide"), and the node's own
 * title underneath, translated.
 *
 * ## Why the eyebrow (`plainKind`) is keyed by group, not by type
 *
 * Because that is the axis the phrasing actually varies on, and the groups are
 * already data: `apps/flows/schema/nodes.py` assigns every type to one of
 * content / logic / actions / other, and those four are exactly the four
 * phrases the design draws. Keying by type would mean a new entry for every
 * node type ever added and a blank eyebrow the day somebody forgot one.
 *
 * ## Why TypeScript and not the Python registry
 *
 * Three reasons, in order of weight. The registry's labels are a different
 * register and should stay literal. A new field on `NodeSpec` changes the
 * committed artefact at `static/flows/flow-schema.json`, so a copy tweak would
 * mean `make schema`, a re-commit and a look at the export tests. And the
 * mapping is not 1:1 with types anyway — it is group-shaped, and the group is
 * already exported.
 *
 * `plain.test.ts` asserts every registered type and group resolves to a
 * non-empty phrase, so a group added in Python without copy here is a red build
 * rather than a blank line on a card.
 */

import i18n from "../i18n";
import { FALLBACK_GROUP, groupOf } from "./artifact";
import type { NodeTypeSpec } from "./types";

/**
 * One phrase per palette group, written to sit above the step's own title.
 *
 * Getters, not plain string values: a frozen object built at module load
 * would read in whatever language was active before main.tsx's
 * setBuilderLocale() ever ran, the same trap publishState.ts's saveCopy()
 * avoids. A getter re-reads i18n.t() on every access, so `GROUP_PHRASE[key]`
 * still indexes like a plain `Record<string, string>` — which is what
 * plain.test.ts exercises — while staying current with the active language.
 */
export const GROUP_PHRASE: Record<string, string> = {
  get content() {
    return i18n.t("plain.group.content");
  },
  // Not "If they reply", which the artboards used: this group holds
  // `condition`, `smart_delay`, `randomizer` and `start_flow`, and only one of
  // them has anything to do with a reply. As a card eyebrow it was merely odd;
  // as the heading over those four in the Add a step menu it was wrong.
  get logic() {
    return i18n.t("plain.group.logic");
  },
  get actions() {
    return i18n.t("plain.group.actions");
  },
  get other() {
    return i18n.t("plain.group.other");
  },
};

/**
 * The handful of types their group's phrase is wrong for.
 *
 * `smart_delay` and `start_flow` are both `logic`, and "Then decide" is close
 * but not right for either — one waits and the other hands over. `note` is `content`
 * but is not a send. Everything else falls through to its group, which is what
 * keeps a node type registered by a later layer readable with no edit here.
 */
export const TYPE_PHRASE: Record<string, string> = {
  get smart_delay() {
    return i18n.t("plain.type.smartDelay");
  },
  get start_flow() {
    return i18n.t("plain.type.startFlow");
  },
  get note() {
    return i18n.t("plain.type.note");
  },
};

/** The eyebrow for a node type: what kind of step this is, in plain words. */
export function plainKind(spec: NodeTypeSpec | undefined, type: string): string {
  // `?? FALLBACK` twice over, deliberately: a group registered in Python with
  // no phrase here reads "Also" rather than rendering an empty eyebrow, which
  // is what plain.test.ts is there to catch before anybody sees it.
  const fallback = GROUP_PHRASE[FALLBACK_GROUP] ?? i18n.t("plain.stepFallback");
  const byType = TYPE_PHRASE[type];
  if (byType) return byType;
  if (!spec) return fallback;
  return GROUP_PHRASE[groupOf(spec)] ?? fallback;
}

/**
 * The node's own name and description, in the reader's words.
 *
 * `plainKind()`'s eyebrow answers "what KIND of step is this" at group
 * granularity — every `send_*` type reads "Then send". The add-step menu and
 * the empty-preview sentence need more than that: they are naming or
 * distinguishing ONE type, and a menu whose "Send a message"/"Send a
 * text"/"Send an email" rows all read the same eyebrow is not choosable. This
 * is that: a full name and description per type, resolved fresh on every
 * call the same way `GROUP_PHRASE`/`TYPE_PHRASE` are, and for the same reason
 * — see this file's own top comment for why it is TypeScript and not the
 * Python registry.
 *
 * Falls through to the registry's own `spec.label`/`spec.description` —
 * never to a blank string — so a node type registered in a later layer with
 * no translation entry yet is still nameable, in English, rather than
 * invisible. Never touches `static/flows/flow-schema.json` itself: that stays
 * the registry's own developer copy, read by the API and the exporter.
 */
export function nodeTypeLabel(spec: NodeTypeSpec | undefined, type: string): string {
  const key = `nodeTypes.${type}.label`;
  const rendered = i18n.t(key);
  return rendered === key ? (spec?.label ?? type) : rendered;
}

/** The node's description, in the reader's words — see {@link nodeTypeLabel}. */
export function nodeTypeDescription(spec: NodeTypeSpec | undefined, type: string): string | undefined {
  if (!spec?.description) return undefined;
  const key = `nodeTypes.${type}.description`;
  const rendered = i18n.t(key);
  return rendered === key ? spec.description : rendered;
}

/**
 * The trigger card's eyebrow.
 *
 * A trigger is not a node and has no group, but it reads as the first step of
 * the flow and the design gives it the same treatment. A function, not a
 * constant, for the same reason GROUP_PHRASE's values are getters.
 */
export function triggerPhrase(): string {
  return i18n.t("plain.trigger");
}
