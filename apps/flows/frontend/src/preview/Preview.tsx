/**
 * The phone mock: the selected step as the person on the other end sees it.
 *
 * The builder could tell you a step was valid and never what it would look
 * like. Everything about a message that matters to the reader — how long it is,
 * whether the buttons fit, whether the tone is right — is a property of the
 * rendered thing, and the only way to see it was to publish and message
 * yourself.
 *
 * What it does NOT do is substitute placeholders. `{{ first_name }}` is shown
 * literally, because that is what apps/flows/rendering.py does at send time
 * with an unknown name, and inventing "Marta" here would preview a different
 * message. It is also the safe reading: nothing in a config is ever evaluated,
 * only displayed.
 */
import { useTranslation } from "react-i18next";

import { nodeSpec } from "../schema/artifact";
import { useBuilder } from "../store/context";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function records(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value) ? value.filter(isRecord) : [];
}

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

/** The buttons a block carries, under whichever key its schema uses. */
function labels(block: Record<string, unknown>, key: string): string[] {
  return records(block[key])
    .map((entry) => text(entry["label"]) || text(entry["title"]))
    .filter(Boolean);
}

export function Preview() {
  const { t } = useTranslation();
  const selected = useBuilder((state) => state.selection.nodes);
  const nodeId = selected.length === 1 ? (selected[0] as string) : null;
  const nodeType = useBuilder((state) => (nodeId ? state.nodeType[nodeId] : undefined));
  const config = useBuilder((state) => (nodeId ? state.config[nodeId] : undefined));

  return (
    <section className="fb-preview" aria-label={t("preview.label")}>
      <p className="fb-section-label">{t("preview.sectionLabel")}</p>
      <div className="fb-phone">
        <div className="fb-phone-screen">
          {nodeId && nodeType ? (
            <Bubbles type={nodeType} config={config} />
          ) : (
            <p className="fb-phone-empty">{t("preview.pickAStep")}</p>
          )}
        </div>
      </div>
      <p className="fb-preview-note">
        {t("preview.placeholderNotePrefix")} <code>{"{{ first_name }}"}</code> {t("preview.placeholderNoteSuffix")}
      </p>
    </section>
  );
}

function Bubbles({ type, config }: { type: string; config: unknown }) {
  const { t } = useTranslation();
  const blocks = isRecord(config) ? records(config["blocks"]) : [];

  if (blocks.length === 0) {
    // Not every step is a message. A wait, a tag, a branch — say what it does
    // rather than drawing an empty phone, which reads as a broken preview.
    const prompt = isRecord(config) ? text(config["prompt"]) || text(config["question"]) : "";
    if (prompt) {
      return <Bubble text={prompt} />;
    }
    const label = nodeSpec(type)?.label ?? type;
    return <p className="fb-phone-empty">{t("preview.doesNotSend", { label })}</p>;
  }

  return (
    <>
      {blocks.map((block, index) => {
        const kind = text(block["type"]);
        const body = text(block["text"]);
        const buttons = [...labels(block, "buttons"), ...labels(block, "quick_replies")];

        if (kind === "text" || body) {
          return <Bubble key={index} text={body} buttons={buttons} />;
        }
        return (
          <div key={index} className="fb-phone-attachment">
            {kind ? `${kind.charAt(0).toUpperCase()}${kind.slice(1)}` : t("preview.attachment")}
          </div>
        );
      })}
    </>
  );
}

function Bubble({ text: body, buttons = [] }: { text: string; buttons?: string[] }) {
  const { t } = useTranslation();
  return (
    <div className="fb-phone-turn">
      <div className="fb-phone-bubble">
        {body || <span className="fb-phone-placeholder">{t("preview.noMessageYet")}</span>}
      </div>
      {buttons.length > 0 ? (
        <div className="fb-phone-buttons">
          {buttons.map((label, index) => (
            <span key={index} className="fb-phone-button">
              {label}
            </span>
          ))}
        </div>
      ) : null}
    </div>
  );
}
