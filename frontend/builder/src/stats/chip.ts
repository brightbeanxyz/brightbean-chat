/**
 * What the stats chip on a node card shows.
 *
 * Kept out of the component so the arithmetic — which is where a click-through
 * rate goes wrong — is testable without mounting a canvas.
 *
 * The CTR is shown "where buttons exist" (issue #26). Two ways a node can
 * qualify, because the counter has two sources: a URL button, which
 * `apps/analytics/tracking.py` wraps on every platform; and a link inside an
 * authored email body, which it wraps only for a workspace that opted in — and
 * which nothing in the graph makes visible from here. So a node that has
 * recorded clicks also qualifies, which is the honest reading of "there is
 * something to divide".
 */
import type { NodeStats } from "../schema/types";

export interface ChipValues {
  sent: number;
  delivered: number;
  clicked: number;
  /** Percentage of sends that were clicked, or null when there is nothing to divide. */
  ctr: number | null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** An object carrying a non-empty `url` — the shape both link schemas share. */
function isLink(value: unknown): boolean {
  return (
    isRecord(value) &&
    typeof value["url"] === "string" &&
    value["url"].length > 0
  );
}

/**
 * Does this node's config carry at least one URL button, at any depth?
 *
 * Matched by the **key that holds the link**, not by "an object with a url in
 * it", because the graph schema has two link shapes and one lookalike
 * (apps/flows/schema/nodes.py):
 *
 * - `buttons: [{id, label, action: "url", url}]` on a message;
 * - `url_button: {label, url}` on a card or a gallery card — no `id` at all,
 *   which an id-based test silently missed;
 * - `{type: "image", url}` on a media block, which is an `<img src>` and is
 *   never wrapped by `apps/analytics/tracking.py`. Keying on the parent is what
 *   keeps it out.
 *
 * The recursion carries the search into `blocks` and `cards` without either
 * being named here, so a future container of cards is covered on arrival.
 */
export function hasUrlButton(config: unknown): boolean {
  if (Array.isArray(config)) {
    return config.some(hasUrlButton);
  }
  if (!isRecord(config)) {
    return false;
  }
  if (isLink(config["url_button"])) {
    return true;
  }
  const buttons = config["buttons"];
  if (Array.isArray(buttons) && buttons.some(isLink)) {
    return true;
  }
  return Object.values(config).some(hasUrlButton);
}

export function chipValues(stats: NodeStats, config: unknown): ChipValues {
  const trackable = stats.clicked > 0 || hasUrlButton(config);
  return {
    sent: stats.sent,
    delivered: stats.delivered,
    clicked: stats.clicked,
    // One decimal, and never a rate with no denominator: "0.0%" of nothing sent
    // reads as a failure rather than as an absence.
    ctr:
      trackable && stats.sent > 0
        ? Math.round((1000 * stats.clicked) / stats.sent) / 10
        : null,
  };
}
