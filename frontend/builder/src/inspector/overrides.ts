/**
 * Which paths and which shapes get a hand-written widget.
 *
 * Two coordinate systems, because the schema has two kinds of "this field is
 * special":
 *
 * * **`<node_type>:<config path>`** — node-specific intent. `randomizer:paths`
 *   wants a weight editor; an array of the same shape elsewhere would not.
 *   Array indexes normalise to `[]`, so one key covers every item.
 * * **`$<defs name>[.<property>]`** — shape-specific, and therefore follows the
 *   shape wherever it is used. `$condition_filter` is the whole config of the
 *   condition node *and* embeddable elsewhere; `$action_set_field.field` is a
 *   field name whether or not the action node is the thing holding it.
 *
 * Lookup order is most-specific-first, and anything with no entry falls through
 * to the generated form — which is what makes a node type added by a later
 * layer configurable with no edit here.
 */
import type { ComponentType } from "react";

import type { ConfigPath } from "../store/paths";
import type { FieldProps } from "./fields";
import { JsonField } from "./fields";
import { ConditionRuleBuilder } from "./widgets/ConditionRuleBuilder";
import { MediaBlockEditor } from "./widgets/MediaBlockEditor";
import { PlaceholderInput } from "./widgets/PlaceholderInput";
import { MemberMultiSelect, picklistSelect } from "./widgets/PicklistSelect";
import { WeightEditor } from "./widgets/WeightEditor";
import { idMintingList } from "./widgets/ButtonListEditor";

type Widget = ComponentType<FieldProps>;

const TagSelect = picklistSelect("tags", { creatable: true });
const FieldSelect = picklistSelect("custom_fields", { creatable: true });
const SequenceSelect = picklistSelect("sequences", { creatable: true });
const FlowSelect = picklistSelect("flows", { creatable: false });
const MemberSelect = picklistSelect("members", { creatable: false });

export const OVERRIDES: Record<string, Widget> = {
  // ── node-type specific ────────────────────────────────────────────────────
  "send_message:buttons": idMintingList("button"),
  "send_message:quick_replies": idMintingList("quick reply"),
  "randomizer:paths": WeightEditor,
  "start_flow:flow_id": FlowSelect,
  "external_request:body": JsonField,
  "data_collection:target.key": FieldSelect,

  // ── shape specific ────────────────────────────────────────────────────────
  $condition_filter: ConditionRuleBuilder,
  $block_media: MediaBlockEditor,
  "$block_text.text": PlaceholderInput,
  "$block_card.title": PlaceholderInput,
  "$block_card.subtitle": PlaceholderInput,
  "$gallery_card.title": PlaceholderInput,
  "$gallery_card.subtitle": PlaceholderInput,
  "$retry_unmatched.text": PlaceholderInput,
  "$action_add_tag.tag": TagSelect,
  "$action_remove_tag.tag": TagSelect,
  "$action_set_field.field": FieldSelect,
  "$action_set_field.value": PlaceholderInput,
  "$action_clear_field.field": FieldSelect,
  "$action_subscribe_sequence.sequence": SequenceSelect,
  "$action_unsubscribe_sequence.sequence": SequenceSelect,
  "$action_assign_conversation.member": MemberSelect,
  "$action_notify_members.member_ids": MemberMultiSelect,
  "$action_notify_members.text": PlaceholderInput,
};

/** Node-type-scoped paths that want a placeholder-aware text box. */
const PLACEHOLDER_PATHS = new Set([
  "send_sms:text",
  "send_email:subject",
  "send_email:html_body",
  "data_collection:question",
  "data_collection:retry.invalid_text",
  "note:text",
  "external_request:url",
]);

for (const key of PLACEHOLDER_PATHS) {
  OVERRIDES[key] = PlaceholderInput;
}

export interface OverrideQuery {
  nodeType: string;
  path: ConfigPath;
  /** The `$defs` name of the schema being rendered, if it is a `$ref`. */
  selfDefs: string | undefined;
  /** The `$defs` name of the object this field is a property of. */
  parentDefs: string | undefined;
  propertyName: string;
}

/** `blocks[0].text` -> `blocks[].text`, so one key covers every list item. */
export function normalizePath(path: ConfigPath): string {
  let out = "";
  for (const step of path) {
    out += typeof step === "number" ? "[]" : out === "" ? step : `.${step}`;
  }
  return out;
}

function exactPath(path: ConfigPath): string {
  let out = "";
  for (const step of path) {
    out += typeof step === "number" ? `[${step}]` : out === "" ? step : `.${step}`;
  }
  return out;
}

/** The candidate keys for one field, most specific first. */
export function overrideKeys({ nodeType, path, selfDefs, parentDefs, propertyName }: OverrideQuery): string[] {
  const keys = [`${nodeType}:${exactPath(path)}`, `${nodeType}:${normalizePath(path)}`];
  // The parent's name first: `$block_text.text` is more specific about this
  // field than `$block_text` is, and a shape-wide override should not beat a
  // property-level one.
  if (parentDefs) {
    keys.push(`$${parentDefs}.${propertyName}`);
  }
  if (selfDefs) {
    keys.push(`$${selfDefs}`);
  }
  return keys;
}

export function lookupOverride(query: OverrideQuery): Widget | undefined {
  for (const key of overrideKeys(query)) {
    const widget = OVERRIDES[key];
    if (widget) {
      return widget;
    }
  }
  return undefined;
}
