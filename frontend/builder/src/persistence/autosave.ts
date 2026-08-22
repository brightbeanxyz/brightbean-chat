/**
 * Debounced autosave (SPEC §16: 2 s), driven by the store from outside React.
 *
 * Four decisions here are not obvious and are worth stating.
 *
 * **Never abort an in-flight PUT.** The obvious way to supersede a save is to
 * abort it and send the newer graph, but an aborted request may already have
 * committed server-side — leaving the user looking at "unsaved" over a saved
 * draft, and the store holding a stale version number. Instead at most one
 * request is in flight, and the next one fires the moment it lands.
 *
 * **Validation is stamped with the revision it describes.** Otherwise a verdict
 * about a graph two edits ago badges nodes the user has already fixed.
 *
 * **A 422 is not retried.** Nothing was written, and the same bytes will be
 * rejected the same way; the loop stays armed so the next edit tries again.
 *
 * **Read-only never installs this at all.** Not a no-op guard inside it — no
 * subscription and no timer, so there is nothing that could fire.
 */
import type { BuilderStore } from "../store/store";
import { toGraph } from "../store/serialize";
import { graphByteLength } from "../store/serialize";
import { saveGraph } from "../api/flows";
import { ApiError } from "../api/client";
import type { ValidationPayload } from "../schema/types";

export const DEBOUNCE_MS = 2000;

/** SPEC §9.5's ladder is the worker's; this is a UI retry, so it is shorter. */
const BACKOFF_MS = [2000, 4000, 8000, 30000];

export interface Autosave {
  /** Save now, skipping the debounce. Used by Publish. */
  flush: () => Promise<void>;
  stop: () => void;
}

export function installAutosave(store: BuilderStore): Autosave {
  let timer: ReturnType<typeof setTimeout> | null = null;
  let inFlight: Promise<void> | null = null;
  let pending = false;
  let attempt = 0;
  let stopped = false;

  const clear = () => {
    if (timer !== null) {
      clearTimeout(timer);
      timer = null;
    }
  };

  const send = async (): Promise<void> => {
    const state = store.getState();
    const revision = state.revision;
    const graph = toGraph(state);

    // Pre-flight. The server answers 413 for an oversized body, but it can say
    // so locally with a live byte count instead of a round trip.
    if (graphByteLength(graph) > state.limits.max_graph_bytes) {
      store.getState().setSave({
        state: "rejected",
        message: `This flow is too big to save (the limit is ${Math.round(state.limits.max_graph_bytes / 1024)} KB).`,
        issues: [],
      });
      return;
    }

    store.getState().setSave({ state: "saving", message: null });

    try {
      const result = await saveGraph(state.env, graph);
      attempt = 0;
      store.getState().applyValidation(result.validation, revision);
      store.getState().setSave({
        state: store.getState().revision === revision ? "saved" : "dirty",
        version: result.version,
        message: null,
        issues: [],
      });
    } catch (error) {
      // A transport failure — offline, DNS, a dropped connection — arrives as
      // a TypeError, not an ApiError. Rethrowing it would escape as an
      // unhandled rejection and the user would watch "Saving…" forever, which
      // is precisely the case the retry ladder below exists for.
      handleFailure(
        error instanceof ApiError ? error : new ApiError(0, "network_error", "The network request failed."),
        revision,
      );
    }
  };

  const handleFailure = (error: ApiError, revision: number) => {
    if (error.status === 422) {
      // Document-stage errors: nothing was written. Surface them and stop —
      // resending the same bytes would only fail the same way.
      const payload = error.payload as { validation?: ValidationPayload } | null;
      if (payload?.validation) {
        store.getState().applyValidation(payload.validation, revision);
      }
      store.getState().setSave({
        state: "rejected",
        message: "This change cannot be saved. Fix the problems below and it will save automatically.",
        issues: payload?.validation?.errors ?? [],
      });
      attempt = 0;
      return;
    }

    if (error.status === 413 || error.status === 400) {
      store.getState().setSave({ state: "rejected", message: error.message, issues: [] });
      attempt = 0;
      return;
    }

    if (error.status === 403 || error.status === 404) {
      store.getState().setSave({ state: "error", message: error.message, issues: [] });
      attempt = 0;
      return;
    }

    // Transport or 5xx: the graph is still in the store, so keep trying.
    store.getState().setSave({ state: "error", message: `${error.message} Retrying…`, issues: [] });
    const delay = BACKOFF_MS[Math.min(attempt, BACKOFF_MS.length - 1)] ?? 30000;
    attempt += 1;
    schedule(delay);
  };

  const run = () => {
    clear();
    if (stopped) {
      return;
    }
    if (inFlight) {
      // Single flight. The follow-up fires on completion rather than after
      // another full debounce, so a busy editor still converges quickly.
      pending = true;
      return;
    }
    inFlight = send().finally(() => {
      inFlight = null;
      if (pending && !stopped) {
        pending = false;
        run();
      }
    });
  };

  const schedule = (delay: number) => {
    clear();
    timer = setTimeout(run, delay);
  };

  const unsubscribe = store.subscribe(
    (state) => state.revision,
    () => {
      if (stopped) {
        return;
      }
      const save = store.getState().save;
      if (save.state !== "saving") {
        store.getState().setSave({ state: "dirty" });
      }
      schedule(DEBOUNCE_MS);
    },
  );

  const beforeUnload = (event: BeforeUnloadEvent) => {
    const { state } = store.getState().save;
    if (state === "dirty" || state === "saving") {
      event.preventDefault();
    }
  };
  window.addEventListener("beforeunload", beforeUnload);

  return {
    flush: async () => {
      clear();

      // Drain, rather than await once. Completing a request runs the `.finally`
      // that starts the queued follow-up, so a single await can return with a
      // *newer* save still open — and Publish would then post the draft as of
      // the previous one. The loop re-checks after every await and only exits
      // when nothing is left in flight.
      while (inFlight) {
        await inFlight;
      }

      // Whatever is still unsent — the debounce cancelled above, or the last
      // attempt failed — goes now, and is drained the same way.
      if (store.getState().save.state === "dirty" || store.getState().save.state === "error") {
        inFlight = send().finally(() => {
          inFlight = null;
        });
        while (inFlight) {
          await inFlight;
        }
      }
    },
    stop: () => {
      stopped = true;
      clear();
      unsubscribe();
      window.removeEventListener("beforeunload", beforeUnload);
    },
  };
}
