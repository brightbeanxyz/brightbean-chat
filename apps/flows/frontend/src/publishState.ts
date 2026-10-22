/** What the toolbar says about publication, trigger reachability and the draft. */
import i18n from "./i18n";
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

// i18n.t(), not a module-level table: these labels have to read in whatever
// language is active *now*, the same reason Toolbar.tsx's own busyLabel()
// does. i18n.t() called at import time would freeze every label in whatever
// language was active before main.tsx's setBuilderLocale() ever runs.
function saveCopyFor(state: string): string {
  const known: Record<string, string> = {
    clean: i18n.t("publishState.save.clean"),
    dirty: i18n.t("publishState.save.dirty"),
    saving: i18n.t("publishState.save.saving"),
    saved: i18n.t("publishState.save.saved"),
    rejected: i18n.t("publishState.save.rejected"),
    error: i18n.t("publishState.save.error"),
  };
  return known[state] ?? state;
}

const PENDING: ReadonlySet<string> = new Set(UNSAVED);

export function offlineHint(): string {
  return i18n.t("publishState.offlineHint");
}

export function publishView(
  save: SaveSlice,
  flowStatus: string | undefined,
  triggerCount = 0,
  enabledCount = 0,
): PublishView {
  const saveCopy = saveCopyFor(save.state);
  const pending = PENDING.has(save.state);
  const currentVersionPublished = !pending && save.version?.published === true;
  const allOff = triggerCount > 0 && enabledCount === 0;
  // "Draft vN" for a version that has never been live. `published_at` as well
  // as `published` because a flow taken offline has nothing published, and its
  // last live version is not a draft — whereas an edit made since is, and
  // "Set live" will publish it, so that one must still say so.
  const neverLive = save.version !== null && !save.version.published && !save.version.published_at;
  const saveLabel =
    !pending && neverLive && save.version
      ? i18n.t("publishState.draftLabel", { save: saveCopy, version: save.version.version })
      : saveCopy;

  if (flowStatus === "archived") {
    return {
      statusLabel: i18n.t("publishState.archived"),
      statusTone: "plain",
      saveLabel,
      publishLabel: i18n.t("publishState.setLive"),
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
    // "Live", the word the flow list's pill and switch use for the same state
    // (HANDOFF §3: "status pill (Draft / Live vN)"), with the version that is
    // running so an edit-in-progress is visibly not it.
    const live = save.publishedVersion
      ? i18n.t("publishState.liveVersion", { version: save.publishedVersion.version })
      : i18n.t("publishState.live");
    return {
      statusLabel: allOff
        ? i18n.t("publishState.liveTriggersOff", { live })
        : triggerCount === 0
          ? i18n.t("publishState.liveNoTriggers", { live })
          : live,
      statusTone: allOff || triggerCount === 0 ? "warning" : "success",
      saveLabel,
      publishLabel:
        action === "enable"
          ? i18n.t("publishState.turnOnTriggers")
          : action === "offline"
            ? i18n.t("toolbar.setOffline")
            : i18n.t("publishState.setLive"),
      publishHint: action === "offline" ? offlineHint() : null,
      action,
      secondary: action === "offline" ? null : "offline",
    };
  }

  if (flowStatus === "offline") {
    return {
      statusLabel: i18n.t("publishState.offline"),
      statusTone: "warning",
      saveLabel,
      publishLabel: i18n.t("publishState.setLive"),
      publishHint: null,
      action: "publish",
      secondary: null,
    };
  }

  return {
    statusLabel: i18n.t("publishState.draft"),
    statusTone: "plain",
    saveLabel,
    publishLabel: i18n.t("publishState.setLive"),
    publishHint: null,
    action: "publish",
    secondary: null,
  };
}
