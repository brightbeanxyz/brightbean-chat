/**
 * The compact summary on each node card.
 *
 * A registry keyed by node type, not a switch: a type this bundle predates
 * falls through to `DefaultPreview`, which derives something informative from
 * the schema alone. That is what lets a node type registered in a later layer
 * be genuinely usable on the canvas with no code here.
 */
import type { ReactNode } from "react";

import i18n from "../i18n";
import { variantLabel } from "../inspector/copy";
import { configSchema } from "../schema/artifact";
import { deref, isTaggedUnion, typesOf } from "../schema/resolve";
import type { JsonSchema, Picklists } from "../schema/types";

export interface PreviewProps {
  config: unknown;
  picklists: Picklists;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function list(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value) ? value.filter(isRecord) : [];
}

function truncate(text: string, length = 90): string {
  return text.length > length ? `${text.slice(0, length)}…` : text;
}

function Pills({ items, quick = false }: { items: string[]; quick?: boolean }) {
  if (items.length === 0) {
    return null;
  }
  const shown = items.slice(0, 3);
  return (
    <div className="flex flex-wrap gap-1 mt-1">
      {shown.map((label, index) => (
        <span key={index} className={quick ? "fb-pill fb-pill-quick" : "fb-pill"}>
          {label || i18n.t("previews.untitled")}
        </span>
      ))}
      {items.length > shown.length ? <span className="fb-pill">+{items.length - shown.length}</span> : null}
    </div>
  );
}

function Empty({ children }: { children: ReactNode }) {
  return <span className="fb-empty">{children}</span>;
}

function SendMessagePreview({ config }: PreviewProps) {
  const blocks = list(isRecord(config) ? config["blocks"] : undefined);
  const first = blocks[0];
  const text = blocks.find((block) => block["type"] === "text");

  return (
    <>
      {text && typeof text["text"] === "string" ? (
        <p>{truncate(text["text"])}</p>
      ) : first ? (
        <Empty>
          {String(first["type"] ?? "block")}
          {blocks.length > 1 ? ` +${blocks.length - 1}` : ""}
        </Empty>
      ) : (
        <Empty>{i18n.t("previews.sendMessage.nothingToSend")}</Empty>
      )}
      <Pills items={list(isRecord(config) ? config["buttons"] : undefined).map((b) => String(b["label"] ?? ""))} />
      <Pills
        quick
        items={list(isRecord(config) ? config["quick_replies"] : undefined).map((q) => String(q["label"] ?? ""))}
      />
    </>
  );
}

/**
 * How to word one operator on a card.
 *
 * Only the symbols, which do not read as words and so have no place in
 * `enumLabels`. Everything else goes through the same `variantLabel` table
 * the condition-rule editor uses (`inspector/copy.ts`) — the operator on the
 * preview and the operator in the dropdown are the same fact, and a table
 * this bundle already has to translate that fact into is the one both
 * should read from, not a second one that only ever gets the English word.
 * An operator apps/contacts/conditions.py adds later, with no entry in
 * either table, still falls through to a humanised form.
 */
const OPERATOR_SYMBOLS: Record<string, string> = {
  "!=": "\u2260",
  ">=": "\u2265",
  "<=": "\u2264",
};

export function operatorCopy(op: unknown): string {
  const key = String(op ?? "");
  return OPERATOR_SYMBOLS[key] ?? variantLabel(key);
}

function ConditionPreview({ config }: PreviewProps) {
  const rules = list(isRecord(config) ? config["rules"] : undefined);
  const match = i18n.t(
    isRecord(config) && config["match"] === "any" ? "previews.condition.anyOf" : "previews.condition.allOf",
  );
  if (rules.length === 0) {
    return <Empty>{i18n.t("previews.condition.nothingToCheck")}</Empty>;
  }
  return (
    <>
      <p className="font-medium">{match}</p>
      <ul>
        {rules.slice(0, 2).map((rule, index) => (
          <li key={index}>
            {String(rule["key"] || rule["source"] || "?")} {operatorCopy(rule["op"])}{" "}
            {rule["value"] === undefined ? "" : String(rule["value"])}
          </li>
        ))}
      </ul>
      {rules.length > 2 ? <Empty>{i18n.t("previews.condition.more", { count: rules.length - 2 })}</Empty> : null}
    </>
  );
}

function RandomizerPreview({ config }: PreviewProps) {
  const paths = list(isRecord(config) ? config["paths"] : undefined);
  const total = paths.reduce((sum, path) => sum + (typeof path["weight"] === "number" ? path["weight"] : 0), 0);
  if (paths.length === 0) {
    return <Empty>{i18n.t("previews.randomizer.noPaths")}</Empty>;
  }
  return (
    <>
      <div className="flex h-2 rounded-full overflow-hidden" style={{ background: "var(--surface-2)" }}>
        {paths.map((path, index) => (
          <div
            key={index}
            style={{
              width: `${total > 0 ? ((Number(path["weight"]) || 0) / total) * 100 : 100 / paths.length}%`,
              background: index % 2 === 0 ? "var(--flow-accent)" : "var(--border-hover)",
            }}
          />
        ))}
      </div>
      <Pills items={paths.map((path) => `${path["weight"] ?? 0}%`)} />
      {total !== 100 ? <Empty>{i18n.t("previews.randomizer.weightsTotal", { total })}</Empty> : null}
    </>
  );
}

function verbCopy(verb: string): string {
  const key = `previews.action.verb.${verb}`;
  const rendered = i18n.t(key);
  return rendered === key ? verb : rendered;
}

function ActionPreview({ config }: PreviewProps) {
  const actions = list(isRecord(config) ? config["actions"] : undefined);
  if (actions.length === 0) {
    return <Empty>{i18n.t("previews.action.nothingToDo")}</Empty>;
  }
  return (
    <Pills
      items={actions.map((action) => {
        const verb = String(action["verb"] ?? "");
        const subject = action["tag"] ?? action["field"] ?? action["sequence"] ?? action["member"] ?? "";
        return `${verbCopy(verb)}${subject ? ` ${subject}` : ""}`;
      })}
    />
  );
}

function SmartDelayPreview({ config }: PreviewProps) {
  if (!isRecord(config)) {
    return <Empty>{i18n.t("previews.nothingSet")}</Empty>;
  }
  if (config["mode"] === "date" && isRecord(config["date"])) {
    const date = config["date"];
    const value = String(date["field"] ?? date["datetime"] ?? i18n.t("previews.smartDelay.aDate"));
    return <p>{i18n.t("previews.smartDelay.waitUntil", { value })}</p>;
  }
  if (isRecord(config["duration"])) {
    const duration = config["duration"];
    return (
      <p>
        {i18n.t("previews.smartDelay.wait", {
          value: String(duration["value"] ?? "?"),
          unit: String(duration["unit"] ?? ""),
        })}
      </p>
    );
  }
  return <Empty>{i18n.t("previews.nothingSet")}</Empty>;
}

function DataCollectionPreview({ config }: PreviewProps) {
  if (!isRecord(config)) {
    return <Empty>{i18n.t("previews.nothingSet")}</Empty>;
  }
  const target = isRecord(config["target"]) ? String(config["target"]["key"] ?? "") : "";
  return (
    <>
      <p>{truncate(String(config["question"] ?? ""))}</p>
      <Pills items={[String(config["reply_type"] ?? "text"), target].filter(Boolean)} />
    </>
  );
}

function ExternalRequestPreview({ config }: PreviewProps) {
  if (!isRecord(config)) {
    return <Empty>{i18n.t("previews.nothingSet")}</Empty>;
  }
  const url = String(config["url"] ?? "");
  let shown = url;
  try {
    // A URL full of {{placeholders}} will not parse; the raw prefix is the
    // honest fallback rather than an empty card.
    shown = new URL(url).host;
  } catch {
    shown = truncate(url, 40);
  }
  return (
    <p>
      <span className="fb-pill">{String(config["method"] ?? "GET")}</span> {shown}
    </p>
  );
}

function StartFlowPreview({ config, picklists }: PreviewProps) {
  const id = isRecord(config) ? String(config["flow_id"] ?? "") : "";
  const target = picklists.flows.find((flow) => flow.id === id);
  if (!id) {
    return <Empty>{i18n.t("previews.startFlow.noFlowPicked")}</Empty>;
  }
  return target ? (
    <p>{target.label}</p>
  ) : (
    <Empty>{i18n.t("previews.startFlow.unknownFlow", { id: truncate(id, 20) })}</Empty>
  );
}

function TextFieldPreview(key: string) {
  return function Preview({ config }: PreviewProps) {
    const value = isRecord(config) ? config[key] : undefined;
    return typeof value === "string" && value ? (
      <p>{truncate(value)}</p>
    ) : (
      <Empty>{i18n.t("previews.textField.empty")}</Empty>
    );
  };
}

/**
 * What an unknown node type gets: up to three of its required scalar fields,
 * read straight off the schema. Not as good as a hand-written preview, but far
 * better than a blank card, and it costs the later layer nothing.
 */
export function DefaultPreview({ config, type }: PreviewProps & { type: string }) {
  const schema = deref(configSchema(type));
  if (!isRecord(config)) {
    return <Empty>{i18n.t("previews.nothingSet")}</Empty>;
  }
  if (isTaggedUnion(schema)) {
    const tag = schema?.discriminator ? config[schema.discriminator.propertyName] : undefined;
    return tag ? <span className="fb-pill">{String(tag)}</span> : <Empty>{i18n.t("previews.nothingSet")}</Empty>;
  }

  const scalar = (property: JsonSchema | undefined) => {
    const types = typesOf(deref(property));
    return types.some((candidate) => ["string", "number", "integer", "boolean"].includes(candidate));
  };

  const shown = (schema?.required ?? [])
    .filter((key) => scalar(schema?.properties?.[key]))
    .slice(0, 3)
    .map((key) => `${key}: ${String(config[key] ?? "")}`);

  return shown.length > 0 ? <Pills items={shown} /> : <Empty>{i18n.t("previews.default.ready")}</Empty>;
}

const PREVIEWS: Record<string, (props: PreviewProps) => ReactNode> = {
  send_message: SendMessagePreview,
  condition: ConditionPreview,
  randomizer: RandomizerPreview,
  action: ActionPreview,
  smart_delay: SmartDelayPreview,
  data_collection: DataCollectionPreview,
  external_request: ExternalRequestPreview,
  start_flow: StartFlowPreview,
  send_sms: TextFieldPreview("text"),
  send_email: TextFieldPreview("subject"),
  note: TextFieldPreview("text"),
};

export function NodePreview({ type, ...props }: PreviewProps & { type: string }) {
  const Preview = PREVIEWS[type];
  return Preview ? <>{Preview(props)}</> : <DefaultPreview {...props} type={type} />;
}
