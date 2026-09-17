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
    fireEvent.click(screen.getByRole("button", { name: "Set live" }));

    await waitFor(() => expect(order).toEqual(["flush", "publish"]), SETTLE);
  });

  it("records the published version on success", async () => {
    http.route("/publish/", { body: published });
    const store = makeStore(makeDetail(makeSampleGraph()));

    renderWith(store, <Toolbar autosave={null} />);
    fireEvent.click(screen.getByRole("button", { name: "Set live" }));

    await waitFor(() => expect(store.getState().save.publishedVersion?.version).toBe(2), SETTLE);
  });

  it("surfaces a 422 as a blocked publish rather than a silent no-op", async () => {
    http.route("/publish/", {
      status: 422,
      body: { validation: { errors: [{ code: "no_entry_node", message: "No entry node." }], warnings: [] } },
    });
    const store = makeStore(makeDetail(makeSampleGraph()));

    renderWith(store, <Toolbar autosave={null} />);
    fireEvent.click(screen.getByRole("button", { name: "Set live" }));

    await waitFor(() => expect(store.getState().save.message).toContain("Not set live"), SETTLE);
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

    expect(screen.queryByRole("button", { name: "Set live" })).toBeNull();
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
    fireEvent.click(screen.getByRole("button", { name: "Set live" }));

    await waitFor(() => expect(store.getState().save.message).toContain("could not be saved"), SETTLE);
    expect(http.requests.filter((request) => request.url.includes("/publish/"))).toHaveLength(0);
    expect(store.getState().save.publishedVersion).toBeNull();
  });

  it("publishes when the flush confirms the server has the draft", async () => {
    const autosave = { flush: vi.fn(async () => true), stop: vi.fn() };
    http.route("/publish/", { body: published });
    const store = makeStore(makeDetail(makeSampleGraph()));

    renderWith(store, <Toolbar autosave={autosave} />);
    fireEvent.click(screen.getByRole("button", { name: "Set live" }));

    await waitFor(() => expect(store.getState().save.publishedVersion?.version).toBe(2), SETTLE);
  });

  it("says Live without a reload as soon as the publish lands", async () => {
    http.route("/publish/", { body: published });

    renderWith(makeStore(makeDetail(makeSampleGraph())), <Toolbar autosave={null} />);
    fireEvent.click(screen.getByRole("button", { name: "Set live" }));

    await waitFor(() => expect(screen.getByText("Live · v2")).toBeTruthy(), SETTLE);
  });

  it("stops saying Archived once the publish that un-archived it lands", async () => {
    /**
     * services.publish() moves an archived flow back to ACTIVE and the response
     * carries the status it landed on. The handler used to update only the save
     * slice, so the store kept the stale `archived` and this header went on
     * offering Publish for a flow that was already live.
     */
    http.route("/publish/", { body: published });
    const detail = makeDetail(makeSampleGraph());
    const store = makeStore({ ...detail, flow: { ...detail.flow, status: "archived" } });

    renderWith(store, <Toolbar autosave={null} />);
    expect(screen.getByText("Archived")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Set live" }));

    await waitFor(() => expect(screen.queryByText("Archived")).toBeNull(), SETTLE);
    expect(screen.getByText("Live · v2")).toBeTruthy();
    expect(store.getState().flow?.status).toBe("active");
  });

  it("says Live on load when the latest version is already published", () => {
    /**
     * The reload case, and the one that made this worth fixing: it reaches the
     * toolbar through load(), never through the publish handler, so a test that
     * publishes by clicking would go on passing while this stayed broken. QA
     * published the same flow twice for exactly this reason.
     */
    const detail = makeDetail(makeSampleGraph(), {
      flow: { id: "flow-1", name: "Welcome", status: "active", folder: "", updated_at: "" },
      version: { id: "v2", version: 2, published: true, updated_at: "" },
      published_version: { id: "v2", version: 2, published: true, updated_at: "" },
    });

    renderWith(makeStore(detail), <Toolbar autosave={null} />);

    expect(screen.getByText("Live · v2")).toBeTruthy();
    expect(screen.queryByText(/Draft v2/)).toBeNull();
  });

  it("stops offering Publish for a version that is already live", () => {
    const detail = makeDetail(makeSampleGraph(), {
      flow: { id: "flow-1", name: "Welcome", status: "active", folder: "", updated_at: "" },
      version: { id: "v2", version: 2, published: true, updated_at: "" },
      published_version: { id: "v2", version: 2, published: true, updated_at: "" },
    });

    renderWith(makeStore(detail), <Toolbar autosave={null} />);

    expect(screen.getByRole("button", { name: "Live" }).hasAttribute("disabled")).toBe(true);
  });

  it("offers it again the moment an edit is pending", async () => {
    const detail = makeDetail(makeSampleGraph(), {
      flow: { id: "flow-1", name: "Welcome", status: "active", folder: "", updated_at: "" },
      version: { id: "v2", version: 2, published: true, updated_at: "" },
      published_version: { id: "v2", version: 2, published: true, updated_at: "" },
    });
    const store = makeStore(detail);

    renderWith(store, <Toolbar autosave={null} />);
    // What installAutosave does on a revision bump (autosave.ts:165-172); there
    // is no autosave mounted here, so drive the same transition directly.
    store.getState().setSave({ state: "dirty" });

    await waitFor(() => expect(screen.getByRole("button", { name: "Set live" }).hasAttribute("disabled")).toBe(false));
  });

  it("fires a success toast the page's global host can render", async () => {
    http.route("/publish/", { body: published });
    const seen: CustomEvent[] = [];
    const listen = (event: Event) => seen.push(event as CustomEvent);
    document.body.addEventListener("showToast", listen);

    try {
      renderWith(makeStore(makeDetail(makeSampleGraph())), <Toolbar autosave={null} />);
      fireEvent.click(screen.getByRole("button", { name: "Set live" }));

      await waitFor(() => expect(seen).toHaveLength(1), SETTLE);
      const detail = seen[0]?.detail as { tone: string; title: string };
      expect(detail.tone).toBe("success");
      expect(detail.title).toBeTruthy();
    } finally {
      document.body.removeEventListener("showToast", listen);
    }
  });

  it("keeps Publish enabled when the flow has known errors", () => {
    /**
     * A regression guard, not a feature test. The module docstring's policy is
     * that Publish stays live with known errors, because the builder only knows
     * what the last save told it. "Disable when already published" must not be
     * read as licence to disable on anything else.
     */
    const detail = makeDetail(makeSampleGraph(), {
      validation: { errors: [{ code: "no_entry_node", message: "The flow has no nodes to run." }], warnings: [] },
    });

    renderWith(makeStore(detail), <Toolbar autosave={null} />);

    expect(screen.getByRole("button", { name: "Set live" }).hasAttribute("disabled")).toBe(false);
  });
});
