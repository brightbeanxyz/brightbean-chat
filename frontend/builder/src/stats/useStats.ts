/**
 * The stats overlay's data.
 *
 * `apps/flows/api.py` answers `available: false` with zeroed totals until L7-A
 * lands, and that distinction is rendered rather than flattened — see
 * StatsChip. Painting zeros that look like real counters is worse than saying
 * there is nothing yet.
 *
 * Polls only while the overlay is on and the tab is visible: a background tab
 * quietly hitting an endpoint every thirty seconds is a cost nobody asked for.
 */
import { useEffect } from "react";

import { fetchStats } from "../api/flows";
import { useBuilder, useBuilderStore } from "../store/context";

const POLL_MS = 30_000;

export function useStats(): void {
  const store = useBuilderStore();
  const visible = useBuilder((state) => state.statsVisible);

  useEffect(() => {
    if (!visible) {
      store.getState().setStats(null);
      return;
    }

    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const poll = async () => {
      if (document.visibilityState !== "visible") {
        timer = setTimeout(() => void poll(), POLL_MS);
        return;
      }
      try {
        const payload = await fetchStats(store.getState().env);
        if (!cancelled) {
          store.getState().setStats(payload);
        }
      } catch {
        // A failing overlay must never take the canvas with it.
      }
      if (!cancelled) {
        timer = setTimeout(() => void poll(), POLL_MS);
      }
    };

    void poll();
    return () => {
      cancelled = true;
      if (timer !== null) {
        clearTimeout(timer);
      }
    };
  }, [visible, store]);
}
