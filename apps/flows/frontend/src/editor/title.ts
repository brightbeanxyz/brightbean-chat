/**
 * A step's own title: the first thing in it that reads like a name.
 *
 * The registry's label ("Send Message") names the type, and a flow with four
 * sends is four rows reading "Send Message". So the title is taken from the
 * step's content where the content has words in it — the first line of the
 * message, the question being asked, the tag being added — and falls back to
 * the type's label when it has none yet.
 *
 * Derived rather than stored. A `title` field on every node would be one more
 * thing to keep in step with the content it duplicates, and an author who
 * renamed the step but not the message would be shown the stale one.
 */
import { nodeSpec } from "../schema/artifact";

const MAX = 48;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function clip(text: string): string {
  const line = text.trim().split("\n")[0]?.trim() ?? "";
  return line.length > MAX ? `${line.slice(0, MAX)}…` : line;
}

/** The first run of text this config carries, wherever it keeps it. */
function textIn(config: unknown): string {
  if (!isRecord(config)) {
    return "";
  }
  const blocks = config["blocks"];
  if (Array.isArray(blocks)) {
    for (const block of blocks) {
      if (isRecord(block) && typeof block["text"] === "string" && block["text"].trim()) {
        return block["text"];
      }
    }
  }
  for (const key of ["prompt", "question", "text", "note", "name"]) {
    const value = config[key];
    if (typeof value === "string" && value.trim()) {
      return value;
    }
  }
  return "";
}

export function titleOf(type: string, config: unknown): string {
  const found = clip(textIn(config));
  return found || nodeSpec(type)?.label || type;
}
