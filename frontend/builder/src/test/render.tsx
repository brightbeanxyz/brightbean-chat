/** Mounting the island, or a fragment of it, against a store under test. */
import { ReactFlowProvider } from "@xyflow/react";
import { render, type RenderResult } from "@testing-library/react";
import type { ReactNode } from "react";

import type { BuilderEnv } from "../env";
import { BuilderStoreProvider } from "../store/context";
import { createBuilderStore, type BuilderStore } from "../store/store";
import { makeDetail } from "./fixtures";
import type { FlowDetail } from "../schema/types";

export const TEST_ENV: BuilderEnv = {
  flowId: "flow-1",
  canEdit: true,
  detailUrl: "/w/ws/api/flows/flow-1/",
  publishUrl: "/w/ws/api/flows/flow-1/publish/",
  statsUrl: "/w/ws/api/flows/flow-1/stats/",
  schemaUrl: "/w/ws/api/flows/schema/",
  mediaPickerUrl: "/w/ws/media/picker/",
};

export function makeStore(detail: FlowDetail | null = makeDetail(), env: Partial<BuilderEnv> = {}): BuilderStore {
  const store = createBuilderStore({ ...TEST_ENV, ...env });
  if (detail) {
    store.getState().load(detail);
  }
  return store;
}

/**
 * The same two providers App.tsx wraps the shell in.
 *
 * ReactFlowProvider is above the canvas rather than inside it, because the
 * palette places a node at the centre of the pane and so needs the same
 * instance — so a fragment rendered here needs it too.
 */
export function renderWith(store: BuilderStore, children: ReactNode): RenderResult {
  return render(
    <BuilderStoreProvider store={store}>
      <ReactFlowProvider>{children}</ReactFlowProvider>
    </BuilderStoreProvider>,
  );
}
