/**
 * Undo, redo, clipboard and delete.
 *
 * Bound here rather than through React Flow's `deleteKeyCode` so the handler
 * can check what has focus: Backspace typed into a config field must edit the
 * text, not delete the node being configured. That is a one-line guard and a
 * genuinely destructive bug without it.
 */
import { useEffect } from "react";

import { clipboardFor, type ClipboardPayload } from "../store/store";
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
        memoryClip = clipboardFor(state, state.selection.nodes);
        void navigator.clipboard?.writeText(JSON.stringify(memoryClip)).catch(() => {});
        return;
      }

      if (!state.env.canEdit) {
        return;
      }

      if (modified && event.key.toLowerCase() === "x") {
        memoryClip = clipboardFor(state, state.selection.nodes);
        void navigator.clipboard?.writeText(JSON.stringify(memoryClip)).catch(() => {});
        state.deleteNodes(state.selection.nodes);
        return;
      }

      if (modified && event.key.toLowerCase() === "v") {
        if (memoryClip) {
          state.paste(memoryClip);
        }
        return;
      }

      if (modified && event.key.toLowerCase() === "d") {
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

    /** Cross-tab and cross-flow paste, when the browser allows it. */
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
    };
  }, [store]);
}

/** Test seam: the module-level clipboard, so a copy/paste pair can be driven. */
export function __setMemoryClip(payload: ClipboardPayload | null): void {
  memoryClip = payload;
}
