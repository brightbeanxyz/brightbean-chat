/**
 * The island's shell: load once, then palette / canvas / inspector.
 *
 * Autosave is installed only when the page says this member may edit. Not
 * disabled inside — never installed, so there is no subscription and no timer
 * that could fire. The API would answer 403 either way; this is the honest UI,
 * not the security boundary.
 */
import { ReactFlowProvider } from "@xyflow/react";
import { useEffect, useMemo, useState } from "react";

import { ApiError } from "./api/client";
import { loadFlow } from "./api/flows";
import { Canvas } from "./canvas/Canvas";
import type { BuilderEnv } from "./env";
import { Inspector } from "./inspector/Inspector";
import { Palette } from "./palette/Palette";
import { installAutosave, type Autosave } from "./persistence/autosave";
import { useStats } from "./stats/useStats";
import { BuilderStoreProvider, useBuilder, useBuilderStore } from "./store/context";
import { createBuilderStore } from "./store/store";
import { ProblemsRail } from "./validation/ProblemsRail";
import { Toolbar } from "./Toolbar";

export function App({ env }: { env: BuilderEnv }) {
  const store = useMemo(() => createBuilderStore(env), [env]);
  return (
    <BuilderStoreProvider store={store}>
      {/* Above the shell, not inside the canvas, so the palette can place a
          node at the centre of the pane rather than only by drag. */}
      <ReactFlowProvider>
        <Shell />
      </ReactFlowProvider>
    </BuilderStoreProvider>
  );
}

function Shell() {
  const store = useBuilderStore();
  const loaded = useBuilder((state) => state.loaded);
  const canEdit = useBuilder((state) => state.env.canEdit);
  const [failure, setFailure] = useState<string | null>(null);
  // State rather than a ref: the toolbar needs the instance as a prop, and a
  // ref assigned inside an effect does not re-render, so Publish would hold a
  // null autosave until something else happened to re-render — and publishing
  // without flushing publishes the *previous* draft.
  const [autosave, setAutosave] = useState<Autosave | null>(null);

  useStats();

  useEffect(() => {
    let cancelled = false;
    void loadFlow(store.getState().env)
      .then((detail) => {
        if (!cancelled) {
          store.getState().load(detail);
        }
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setFailure(error instanceof ApiError ? error.message : "This flow could not be loaded.");
        }
      });
    return () => {
      cancelled = true;
    };
  }, [store]);

  useEffect(() => {
    if (!loaded || !canEdit) {
      return;
    }
    const installed = installAutosave(store);
    setAutosave(installed);
    return () => {
      installed.stop();
      setAutosave(null);
    };
  }, [loaded, canEdit, store]);

  /**
   * Re-check when the tab regains focus.
   *
   * Capability warnings depend on which channels are connected, and #11's
   * trigger drawer can change that in another tab. Re-fetching the detail
   * payload is how those warnings update without the user touching the canvas.
   */
  useEffect(() => {
    if (!loaded) {
      return;
    }
    let last = 0;
    const onFocus = () => {
      const now = Date.now();
      if (now - last < 30_000 || store.getState().save.state !== "clean") {
        return;
      }
      last = now;
      void loadFlow(store.getState().env)
        .then((detail) => store.getState().applyValidation(detail.validation, store.getState().revision))
        .catch(() => {});
    };
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [loaded, store]);

  if (failure) {
    return (
      <div className="h-full flex items-center justify-center p-8">
        <div className="alert-error max-w-lg">
          <p className="font-medium">The flow builder could not load this flow.</p>
          <p className="mt-1">{failure}</p>
        </div>
      </div>
    );
  }

  if (!loaded) {
    return (
      <div className="h-full flex items-center justify-center p-8">
        <p className="text-sm" style={{ color: "var(--text-secondary)" }}>
          Loading the flow builder…
        </p>
      </div>
    );
  }

  return (
    <>
      <Toolbar autosave={autosave} />
      <div className="fb-shell">
        <Palette />
        <Canvas />
        <Inspector />
      </div>
      <ProblemsRail />
    </>
  );
}
