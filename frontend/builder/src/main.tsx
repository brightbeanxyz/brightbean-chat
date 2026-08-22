/**
 * The island's entry point.
 *
 * Everything it needs is a data attribute on the mount div that
 * templates/flows/edit.html renders, so nothing here reverses a URL or knows a
 * workspace id. The server-rendered fallback inside that div stays until the
 * mount succeeds, so a slow parse is not a blank rectangle.
 *
 * Nothing in here is allowed to throw past this file. A bootstrap failure and a
 * render failure both have to end up as a message in the mount div, because the
 * alternative is the "Loading the flow builder…" placeholder sitting there
 * forever with the reason only in the console.
 */
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App";
import { BuilderFailure, ErrorBoundary } from "./ErrorBoundary";
import { readEnv } from "./env";

import "@xyflow/react/dist/style.css";

const mount = document.getElementById("flow-builder");

if (mount) {
  const root = createRoot(mount);
  try {
    const env = readEnv(mount);
    root.render(
      <StrictMode>
        <ErrorBoundary>
          <App env={env} />
        </ErrorBoundary>
      </StrictMode>,
    );
  } catch (error) {
    // readEnv throws when the mount div is missing an attribute — a template
    // change, not something the reader can fix, but they still need to be told
    // rather than left watching a placeholder.
    console.error("The flow builder could not start.", error);
    root.render(<BuilderFailure error={error instanceof Error ? error : new Error(String(error))} />);
  }
}
