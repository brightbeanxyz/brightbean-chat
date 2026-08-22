/**
 * The store, reached from React.
 *
 * Context carries only the store *reference*, which never changes — so this
 * adds no re-renders of its own. Every subscription goes through
 * `useStore(store, selector)`, i.e. `useSyncExternalStore`, which is what makes
 * a component re-render only when the slice it asked for changed. Putting the
 * state itself in context would re-render every consumer on every drag frame.
 */
import { createContext, useContext, type ReactNode } from "react";
import { useStore } from "zustand";

import type { BuilderState, BuilderStore } from "./store";

const StoreContext = createContext<BuilderStore | null>(null);

export function BuilderStoreProvider({ store, children }: { store: BuilderStore; children: ReactNode }) {
  return <StoreContext.Provider value={store}>{children}</StoreContext.Provider>;
}

export function useBuilderStore(): BuilderStore {
  const store = useContext(StoreContext);
  if (!store) {
    throw new Error("useBuilderStore was called outside <BuilderStoreProvider>.");
  }
  return store;
}

export function useBuilder<T>(selector: (state: BuilderState) => T): T {
  return useStore(useBuilderStore(), selector);
}

/** Actions are stable, so reading them this way never causes a re-render. */
export function useActions() {
  return useBuilderStore().getState();
}
