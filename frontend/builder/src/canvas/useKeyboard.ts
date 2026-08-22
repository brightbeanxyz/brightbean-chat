/**
 * Undo, redo, clipboard and delete.
 *
 * Bound here rather than through React Flow's `deleteKeyCode` so the handler
 * can check what has focus: Backspace typed into a config field must edit the
 * text, not delete the node being configured. That is a one-line guard and a
 * genuinely destructive bug without it.
 */
import { useEffect } from "react";

import { clipboardFor, type BuilderState, type ClipboardPayload } from "../store/store";
import { useBuilderStore } from "../store/context";

const CLIP_KIND = "brightbean/flow-clip";

/**
 * The in-memory fallback.
 *
 * `navigator.clipboard` is unavailable in insecure contexts and can reject on
 * permission grounds, so the module variable is the source of truth and the
 * system clipboard is a best-effort bonus that buys cross-tab paste.
 */
let memoryClip: ClipboardPayload | null = null;

/**
 * When the `paste` listener last handled one of our payloads, and the window
 * the keydown fallback waits before deciding the listener is not coming.
 *
 * Chromium and Firefox both fire `paste` after the Cmd+V keydown, so without
 * this handshake every in-app paste inserted the nodes twice.
 */
let pastedAt = 0;
let pendingPasteFallback: number | null = null;
const PASTE_FALLBACK_MS = 50;

/** Capture the selection, in memory always and on the system clipboard if allowed. */
function copySelection(state: BuilderState): void {
  memoryClip = clipboardFor(state, state.selection.nodes);
  void navigator.clipboard?.writeText(JSON.stringify(memoryClip)).catch(() => {});
}

function isTextEntry(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) {
    return false;
  }
  return (
    target.isContentEditable ||
    target instanceof HTMLInputElement ||
    target instanceof HTMLTextAreaElement ||
    target instanceof HTMLSelectElement
  );
}

export function useKeyboard(): void {
  const store = useBuilderStore();

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const state = store.getState();
      const modified = event.metaKey || event.ctrlKey;

      if (isTextEntry(event.target)) {
        return;
      }

      if (modified && event.key.toLowerCase() === "z") {
        event.preventDefault();
        if (state.env.canEdit) {
          (event.shiftKey ? state.redo : state.undo)();
        }
        return;
      }

      if (modified && event.key.toLowerCase() === "c") {
        // An empty selection means the reader is copying something else on the
        // page — a message from the problems rail, say. Writing an empty flow
        // clip here would destroy whatever they actually had on the clipboard.
        if (state.selection.nodes.length > 0) {
          copySelection(state);
        }
        return;
      }

      if (!state.env.canEdit) {
        return;
      }

      if (modified && event.key.toLowerCase() === "x") {
        if (state.selection.nodes.length > 0) {
          copySelection(state);
          state.deleteNodes(state.selection.nodes);
        }
        return;
      }

      if (modified && event.key.toLowerCase() === "v") {
        // Do NOT paste here when the browser is going to deliver a `paste`
        // event as well — both paths call state.paste, so an ordinary Cmd+V
        // would insert the nodes twice. The paste listener is the primary
        // route; this is only the fallback for browsers that never fire it,
        // and `pastedAt` is how the listener tells this branch to stand down.
        pendingPasteFallback = window.setTimeout(() => {
          pendingPasteFallback = null;
          if (memoryClip && Date.now() - pastedAt > PASTE_FALLBACK_MS) {
            store.getState().paste(memoryClip);
          }
        }, PASTE_FALLBACK_MS);
        return;
      }

      if (modified && event.key.toLowerCase() === "d") {
        if (state.selection.nodes.length === 0) {
          return;
        }
        event.preventDefault();
        state.paste(clipboardFor(state, state.selection.nodes));
        return;
      }

      if (event.key === "Backspace" || event.key === "Delete") {
        event.preventDefault();
        if (state.selection.edges.length > 0) {
          state.deleteEdges(state.selection.edges);
        }
        if (state.selection.nodes.length > 0) {
          state.deleteNodes(state.selection.nodes);
        }
      }
    };

    /**
     * The primary paste route: cross-tab, cross-flow, and the ordinary in-app
     * Cmd+V, because the browser fires this after the keydown above.
     *
     * Stamping `pastedAt` is what stops the keydown fallback from adding a
     * second copy.
     */
    const onPaste = (event: ClipboardEvent) => {
      const state = store.getState();
      if (!state.env.canEdit || isTextEntry(event.target)) {
        return;
      }
      const text = event.clipboardData?.getData("text/plain");
      if (!text) {
        return;
      }
      try {
        const payload = JSON.parse(text) as ClipboardPayload;
        if (payload?.kind === CLIP_KIND) {
          event.preventDefault();
          pastedAt = Date.now();
          if (pendingPasteFallback !== null) {
            clearTimeout(pendingPasteFallback);
            pendingPasteFallback = null;
          }
          state.paste(payload);
        }
      } catch {
        // Not our payload. Let the browser have it.
      }
    };

    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("paste", onPaste);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("paste", onPaste);
      if (pendingPasteFallback !== null) {
        clearTimeout(pendingPasteFallback);
        pendingPasteFallback = null;
      }
    };
  }, [store]);
}

/** Test seam: the module-level clipboard, so a copy/paste pair can be driven. */
export function __setMemoryClip(payload: ClipboardPayload | null): void {
  memoryClip = payload;
}
