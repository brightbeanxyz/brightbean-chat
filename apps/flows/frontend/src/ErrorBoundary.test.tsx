/**
 * The failing-bundle case.
 *
 * templates/flows/edit.html covers a *missing* bundle; this covers one that
 * loads and then throws, which previously left the server-rendered "Loading the
 * flow builder…" placeholder on screen for good.
 */
import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ErrorBoundary } from "./ErrorBoundary";
import { MissingEnvError, readEnv } from "./env";

function Exploding(): never {
  throw new Error("the canvas fell over");
}

beforeEach(() => vi.spyOn(console, "error").mockImplementation(() => {}));
afterEach(() => vi.restoreAllMocks());

describe("the error boundary", () => {
  it("replaces a crashed island with something a person can act on", () => {
    render(
      <ErrorBoundary>
        <Exploding />
      </ErrorBoundary>,
    );

    expect(screen.getByText(/stopped working/i)).toBeInTheDocument();
    expect(screen.getByText(/the canvas fell over/)).toBeInTheDocument();
  });

  it("leaves a working island alone", () => {
    render(
      <ErrorBoundary>
        <p>the canvas</p>
      </ErrorBoundary>,
    );

    expect(screen.getByText("the canvas")).toBeInTheDocument();
    expect(screen.queryByText(/stopped working/i)).toBeNull();
  });

  it("still records the failure for whoever opens the console", () => {
    render(
      <ErrorBoundary>
        <Exploding />
      </ErrorBoundary>,
    );

    expect(console.error).toHaveBeenCalled();
  });
});

describe("a mount div missing an attribute", () => {
  it("throws a named error the entry point can render", () => {
    const mount = document.createElement("div");
    mount.setAttribute("data-flow-id", "flow-1");

    expect(() => readEnv(mount)).toThrow(MissingEnvError);
  });
});
