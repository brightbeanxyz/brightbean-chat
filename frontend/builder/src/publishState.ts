/**
 * What the toolbar says about this flow, and whether Publish is worth offering.
 *
 * Pulled out of the component so the cases that are awkward to reach by
 * clicking -- a reload onto an already-published version, an archived flow, an
 * edit in flight over a live one -- are assertable as a table.
 *
 * The rule this file exists to keep: the label describes what the *server* last
 * told us, never what we hope happened. `save.version.published` is the flag
 * the publish response and the detail response both carry, so a reload and a
 * fresh publish arrive at the same label by the same route.
 */
import { UNSAVED } from "./persistence/autosave";
import type { SaveSlice } from "./store/store";

export type PublishTone = "success" | "warning" | "plain";

export interface PublishView {
  /** The status text beside the Publish button. */
  label: string;
  tone: PublishTone;
  /** The live version, when it is not the one the label already names. */
  liveChip: string | null;
  publishDisabled: boolean;
  publishLabel: string;
  /** Why the button is off, for its `title`. Null when it is on. */
  publishHint: string | null;
}

const SAVE_COPY: Record<string, string> = {
  clean: "No changes",
  dirty: "Unsaved changes",
  saving: "Saving…",
  saved: "Saved",
  rejected: "Not saved",
  error: "Save failed",
};

// Borrowed from the autosave rather than restated: "the server does not have
// what you are looking at" is one fact, and two copies of it would disagree the
// first time a save state is added.
const PENDING: ReadonlySet<string> = new Set(UNSAVED);

export function publishView(save: SaveSlice, flowStatus: string | undefined): PublishView {
  const saveCopy = SAVE_COPY[save.state] ?? save.state;
  const pending = PENDING.has(save.state);
  const live = save.publishedVersion;
  const liveNow = !pending && save.version?.published === true;

  if (flowStatus === "archived") {
    // Archiving does not unpublish, and publishing un-archives (services.publish
    // sets status back to ACTIVE), so the button stays live and says so.
    return {
      label: "Archived",
      tone: "warning",
      liveChip: live ? `v${live.version} live` : null,
      publishDisabled: false,
      publishLabel: "Set live",
      publishHint: null,
    };
  }

  if (liveNow) {
    return {
      label: `Live · v${save.version?.version}`,
      tone: "success",
      liveChip: null,
      publishDisabled: true,
      // Still "Set live", not "Live": the status beside it already says
      // `Live · v2`, and a button repeating the word says nothing about what
      // pressing it would do. Disabled plus the hint carries that.
      publishLabel: "Set live",
      publishHint: "This version is already live. Make a change to set it live again.",
    };
  }

  return {
    // No version number while an edit is pending over a published one: the next
    // save opens version n+1, so printing n here would name the *live* version
    // as though it were the draft in front of you.
    label: pending && live ? saveCopy : save.version ? `${saveCopy} · Draft v${save.version.version}` : saveCopy,
    tone: "plain",
    liveChip: live ? `v${live.version} live` : null,
    publishDisabled: false,
    publishLabel: "Set live",
    publishHint: null,
  };
}
