/**
 * Asking the page's trigger drawer to open, from inside the island.
 *
 * Dispatched on `document.body` with `bubbles: true`. Not on `window`: htmx
 * registers its `from:body` listener on body, and an event dispatched *at*
 * window never passes through body on the way anywhere. Bubbling up from body
 * reaches window as well, so Alpine's `.window` handler in edit.html sees it
 * too — which is the one that actually reads the id. See the comment there for
 * why the ordering matters.
 */
export const OPEN_TRIGGER_EVENT = "open-trigger";

export interface OpenTriggerDetail {
  /** The trigger to open, or null for "let me add one". */
  triggerId: string | null;
}

export function openTriggerDrawer(triggerId: string | null): void {
  if (typeof document === "undefined") {
    return;
  }
  document.body.dispatchEvent(
    new CustomEvent<OpenTriggerDetail>(OPEN_TRIGGER_EVENT, {
      detail: { triggerId },
      bubbles: true,
    }),
  );
}
