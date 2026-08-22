/**
 * Publishing.
 *
 * The ordering assertion is the point: apps/flows/api.py publishes whatever
 * draft the server currently holds, so publishing without first flushing a
 * pending autosave publishes the *previous* version while appearing to succeed.
 */
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Toolbar } from "./Toolbar";
import { makeDetail, makeSampleGraph } from "./test/fixtures";
import { installCsrfToken, stubHttp, type HttpStub } from "./test/http";
import { makeStore, renderWith } from "./test/render";

let http: HttpStub;

const published = {
  flow: { id: "flow-1", name: "Welcome", status: "active", folder: "", updated_at: "" },
  version: { id: "v", version: 2, published: true, updated_at: "" },
  validation: { errors: [], warnings: [] },
};

beforeEach(() => {
  http = stubHttp();
  installCsrfToken();
});

afterEach(() => http.restore());

/**
 * Generous, because Testing Library's default is 1 s and these wait on a real
 * promise chain. A loaded CI runner turns a correct test into an intermittent
 * one at that default, and an intermittent test in CI is worse than a slow one.
 */
const SETTLE = { timeout: 5000 };

describe("Publish", () => {
  it("flushes the pending save before it posts", async () => {
    const order: string[] = [];
    const autosave = {
      flush: vi.fn(async () => {
        order.push("flush");
        return true;
      }),
      stop: vi.fn(),
    };
    http.route("/publish/", () => {
      order.push("publish");
      return { body: published };
    });

    renderWith(makeStore(makeDetail(makeSampleGraph())), <Toolbar autosave={autosave} />);
    fireEvent.click(screen.getByRole("button", { name: "Publish" }));

    await waitFor(() => expect(order).toEqual(["flush", "publish"]), SETTLE);
  });

  it("records the published version on success", async () => {
    http.route("/publish/", { body: published });
    const store = makeStore(makeDetail(makeSampleGraph()));

    renderWith(store, <Toolbar autosave={null} />);
    fireEvent.click(screen.getByRole("button", { name: "Publish" }));

    await waitFor(() => expect(store.getState().save.publishedVersion?.version).toBe(2), SETTLE);
  });

  it("surfaces a 422 as a blocked publish rather than a silent no-op", async () => {
    http.route("/publish/", {
      status: 422,
      body: { validation: { errors: [{ code: "no_entry_node", message: "No entry node." }], warnings: [] } },
    });
    const store = makeStore(makeDetail(makeSampleGraph()));

    renderWith(store, <Toolbar autosave={null} />);
    fireEvent.click(screen.getByRole("button", { name: "Publish" }));

    await waitFor(() => expect(store.getState().save.message).toContain("Publish blocked"), SETTLE);
    expect(store.getState().validation.errors[0]?.code).toBe("no_entry_node");
    expect(store.getState().save.publishedVersion).toBeNull();
  });

  it("shows saved state and outstanding problems side by side, never folded into one", async () => {
    // A 200 from PUT means the draft was written; it can still carry errors.
    // "Saved" and "valid" are different questions.
    const store = makeStore(
      makeDetail(makeSampleGraph(), {
        validation: { errors: [{ code: "no_entry_node", message: "No entry node." }], warnings: [] },
      }),
    );

    renderWith(store, <Toolbar autosave={null} />);

    expect(screen.getByText("1 to fix")).toBeInTheDocument();
    expect(screen.getByText(/No changes/)).toBeInTheDocument();
  });

  it("offers no Publish at all when the member cannot edit", () => {
    renderWith(makeStore(makeDetail(makeSampleGraph()), { canEdit: false }), <Toolbar autosave={null} />);

    expect(screen.queryByRole("button", { name: "Publish" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Undo" })).toBeNull();
  });
});

describe("Publish and a flush that did not land", () => {
  it("refuses to publish rather than posting the previous draft", async () => {
    // flush() draining says nothing about the outcome. After a 422, a size
    // preflight or a transport failure the server still holds the older draft,
    // and publishing it while reporting success is the worst of both.
    const autosave = { flush: vi.fn(async () => false), stop: vi.fn() };
    http.route("/publish/", { body: published });
    const store = makeStore(makeDetail(makeSampleGraph()));

    renderWith(store, <Toolbar autosave={autosave} />);
    fireEvent.click(screen.getByRole("button", { name: "Publish" }));

    await waitFor(() => expect(store.getState().save.message).toContain("could not be saved"), SETTLE);
    expect(http.requests.filter((request) => request.url.includes("/publish/"))).toHaveLength(0);
    expect(store.getState().save.publishedVersion).toBeNull();
  });

  it("publishes when the flush confirms the server has the draft", async () => {
    const autosave = { flush: vi.fn(async () => true), stop: vi.fn() };
    http.route("/publish/", { body: published });
    const store = makeStore(makeDetail(makeSampleGraph()));

    renderWith(store, <Toolbar autosave={autosave} />);
    fireEvent.click(screen.getByRole("button", { name: "Publish" }));

    await waitFor(() => expect(store.getState().save.publishedVersion?.version).toBe(2), SETTLE);
  });
});
