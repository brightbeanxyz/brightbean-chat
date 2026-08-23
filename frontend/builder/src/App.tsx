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
   * Re-check when the tab regains focus, or when the trigger drawer changes.
   *
   * Capability warnings are computed from what this flow is *triggered* on, and
   * the drawer that edits triggers is on this same page — so `triggersChanged`,
   * the event its htmx mutations already broadcast on `body`, is the precise
   * moment those warnings and the toolbar's trigger count go stale. `focus`
   * covers the other tab, which is what this effect originally existed for.
   *
   * Both paths re-apply the trigger list as well as the validation, because
   * they change together and for the same reason.
   */
  useEffect(() => {
    if (!loaded) {
      return;
    }
    let last = 0;
    const refresh = (throttle: boolean) => {
      const now = Date.now();
      if ((throttle && now - last < 30_000) || store.getState().save.state !== "clean") {
        return;
      }
      last = now;
      void loadFlow(store.getState().env)
        .then((detail) => {
          store.getState().applyValidation(detail.validation, store.getState().revision);
          store.getState().setTriggers(detail.triggers);
        })
        .catch(() => {});
    };
    const onFocus = () => refresh(true);
    // Not throttled: this one is a direct consequence of something the user
    // just did on this page, and a 30-second window would make their own edit
    // appear not to have registered.
    const onTriggersChanged = () => refresh(false);
    window.addEventListener("focus", onFocus);
    document.body.addEventListener("triggersChanged", onTriggersChanged);
    return () => {
      window.removeEventListener("focus", onFocus);
      document.body.removeEventListener("triggersChanged", onTriggersChanged);
    };
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
