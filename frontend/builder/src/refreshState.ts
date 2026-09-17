/**
 * Whether a background refresh's *graph-derived* results still describe what is
 * on screen.
 *
 * Extracted from App.tsx's refresh effect for the same reason publishState.ts
 * was extracted from the toolbar: the interesting part is a decision with four
 * inputs and no DOM, and reaching it through a mounted builder tests the mount
 * rather than the decision.
 *
 * Triggers are not covered by this. They live in their own table, the drawer
 * edits them while the canvas is dirty, and they are applied unconditionally —
 * refusing them is what made a trigger you just saved wait for a graph save.
 */
import type { SaveState } from "./store/store";

/** Save states in which the server has the revision the store is holding. */
const SYNCED: ReadonlySet<SaveState> = new Set<SaveState>(["clean", "saved"]);

export function refreshApplies(save: SaveState, revisionAtRequest: number, revisionNow: number): boolean {
  // The revision must not have moved while the request was in flight. Otherwise
  // a verdict about the old graph is stamped with the current revision, which
  // is exactly how a stale verdict passes itself off as current.
  if (revisionNow !== revisionAtRequest) {
    return false;
  }
  // And the server must actually have this revision. "clean" alone was the bug:
  // autosave leaves the store "saved", so after the first autosave of a session
  // validation and version metadata stopped refreshing entirely. autosave.ts
  // only sets "saved" when the revision held still across its own save, so it
  // means the same thing "clean" does.
  return SYNCED.has(save);
}
