/**
 * What every generated field needs, without threading it through ten levels of
 * recursion: which node it is editing, whether the canvas is read-only, the
 * pick-lists, and the two mutators.
 */
import { createContext, useContext, type ReactNode } from "react";

import type { BuilderEnv } from "../env";
import type { ConfigPath } from "../store/paths";
import type { Picklists } from "../schema/types";
import type { NormalizedIssue } from "../validation/normalize";

export interface FieldContextValue {
  nodeId: string;
  nodeType: string;
  readOnly: boolean;
  picklists: Picklists;
  issues: readonly NormalizedIssue[];
  env: BuilderEnv;
  /** Write a value at `path`, coalescing history under `historyKey`. */
  set: (path: ConfigPath, value: unknown, historyKey?: string) => void;
  /**
   * Remove the key at `path` entirely.
   *
   * Not "set it to empty": most optional strings carry `minLength: 1` and every
   * object is closed, so writing `""` or `null` turns "cleared" into a
   * validation error.
   */
  clear: (path: ConfigPath) => void;
}

const Context = createContext<FieldContextValue | null>(null);

export function FieldProvider({ value, children }: { value: FieldContextValue; children: ReactNode }) {
  return <Context.Provider value={value}>{children}</Context.Provider>;
}

export function useField(): FieldContextValue {
  const value = useContext(Context);
  if (!value) {
    throw new Error("A schema field was rendered outside <FieldProvider>.");
  }
  return value;
}
