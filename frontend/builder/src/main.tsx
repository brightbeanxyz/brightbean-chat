/**
 * The island's entry point.
 *
 * Everything it needs is a data attribute on the mount div that
 * templates/flows/edit.html renders, so nothing here reverses a URL or knows a
 * workspace id. The server-rendered fallback inside that div stays until the
 * mount succeeds, so a slow parse is not a blank rectangle.
 */
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App";
import { readEnv } from "./env";

import "@xyflow/react/dist/style.css";

const mount = document.getElementById("flow-builder");

if (mount) {
  const env = readEnv(mount);
  createRoot(mount).render(
    <StrictMode>
      <App env={env} />
    </StrictMode>,
  );
}
