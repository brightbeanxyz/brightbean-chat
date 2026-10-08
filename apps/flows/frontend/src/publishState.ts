/** What the toolbar says about publication, trigger reachability and the draft. */
import { UNSAVED } from "./persistence/autosave";
import type { SaveSlice } from "./store/store";

export type PublishTone = "success" | "warning" | "plain";

/**
 * What the toolbar's one button does. Named rather than read back off the
 * label, so rewording a label can never change what clicking it does.
 */
export type PublishAction = "publish" | "enable" | "offline";

export interface PublishView {
  statusLabel: string;
  statusTone: PublishTone;
  saveLabel: string;
  publishLabel: string;
  publishHint: string | null;
  action: PublishAction;
  /**
   * A second, quieter button beside the first. Only ever "offline": a live flow
   * always has a way out, whatever the first button is busy with — "Set live"
   * for a draft that may not even validate yet, or "Turn on triggers" for a
   * flow that still runs for the API, sequences and other flows.
   */
  secondary: "offline" | null;
}

const SAVE_COPY: Record<string, string> = {
  clean: "No changes",
  dirty: "Unsaved changes",
  saving: "Saving…",
  saved: "Saved",
  rejected: "Not saved",
  error: "Save failed",
};

const PENDING: ReadonlySet<string> = new Set(UNSAVED);

export const OFFLINE_HINT = "Stops this flow now, including conversations already in it.";

export function publishView(
  save: SaveSlice,
  flowStatus: string | undefined,
  triggerCount = 0,
  enabledCount = 0,
): PublishView {
  const saveCopy = SAVE_COPY[save.state] ?? save.state;
  const pending = PENDING.has(save.state);
  const currentVersionPublished = !pending && save.version?.published === true;
  const allOff = triggerCount > 0 && enabledCount === 0;
  // "Draft vN" for a version that has never been live. `published_at` as well
  // as `published` because a flow taken offline has nothing published, and its
  // last live version is not a draft — whereas an edit made since is, and
  // "Set live" will publish it, so that one must still say so.
  const neverLive = save.version !== null && !save.version.published && !save.version.published_at;
  const saveLabel = !pending && neverLive && save.version ? `${saveCopy} · Draft v${save.version.version}` : saveCopy;

  if (flowStatus === "archived") {
    return {
      statusLabel: "Archived",
      statusTone: "plain",
      saveLabel,
      publishLabel: "Set live",
      publishHint: null,
      action: "publish",
      secondary: null,
    };
  }

  if (flowStatus === "active") {
    // With nothing new to publish, the button is the way back out: the
    // published version is what is running, so "Set live" had nothing to do
    // and sat there disabled. Every trigger off is the exception — the flow
    // cannot start yet, and turning them on is the more likely next step — and
    // so is a pending draft; both keep "Set offline" as the second button.
    const action: PublishAction = !currentVersionPublished ? "publish" : allOff ? "enable" : "offline";
    return {
      statusLabel: allOff ? "Published · Triggers off" : triggerCount === 0 ? "Published · No triggers" : "Published",
      statusTone: allOff || triggerCount === 0 ? "warning" : "success",
      saveLabel,
      publishLabel: action === "enable" ? "Turn on triggers" : action === "offline" ? "Set offline" : "Set live",
      publishHint: action === "offline" ? OFFLINE_HINT : null,
      action,
      secondary: action === "offline" ? null : "offline",
    };
  }

  if (flowStatus === "offline") {
    return {
      statusLabel: "Offline",
      statusTone: "warning",
      saveLabel,
      publishLabel: "Set live",
      publishHint: null,
      action: "publish",
      secondary: null,
    };
  }

  return {
    statusLabel: "Draft",
    statusTone: "plain",
    saveLabel,
    publishLabel: "Set live",
    publishHint: null,
    action: "publish",
    secondary: null,
  };
}
