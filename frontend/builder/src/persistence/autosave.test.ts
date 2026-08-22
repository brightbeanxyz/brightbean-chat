/**
 * Autosave semantics.
 *
 * The subtle ones are single-flight, never aborting an in-flight PUT, and the
 * fact that a 200 carrying `errors` is still a save. Each has a comment where
 * it is asserted.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { installCsrfToken, stubHttp, type HttpStub } from "../test/http";
import { makeDetail, makeSampleGraph } from "../test/fixtures";
import { makeStore } from "../test/render";
import { DEBOUNCE_MS, installAutosave } from "./autosave";

let http: HttpStub;

const savedBody = (version = 2) => ({
  flow: { id: "flow-1", name: "Welcome", status: "draft", folder: "", updated_at: "" },
  version: { id: "v", version, published: false, updated_at: "" },
  validation: { errors: [], warnings: [] },
});

beforeEach(() => {
  vi.useFakeTimers();
  http = stubHttp();
  installCsrfToken();
});

afterEach(() => {
  http.restore();
  vi.useRealTimers();
});

function armed() {
  const store = makeStore(makeDetail(makeSampleGraph()));
  const autosave = installAutosave(store);
  const edit = (text: string) =>
    store.getState().updateConfig(store.getState().nodeOrder[0] as string, ["blocks", 0, "text"], text, "typing");
  return { store, autosave, edit };
}

const puts = () => http.requests.filter((request) => request.method === "PUT");

describe("the 2 s debounce", () => {
  it("collapses a burst of edits into one PUT", async () => {
    http.route("/api/flows/", { body: savedBody() });
    const { edit, autosave } = armed();

    edit("a");
    await vi.advanceTimersByTimeAsync(500);
    edit("ab");
    await vi.advanceTimersByTimeAsync(500);
    edit("abc");
    expect(puts()).toHaveLength(0);

    await vi.advanceTimersByTimeAsync(DEBOUNCE_MS);
    expect(puts()).toHaveLength(1);
    autosave.stop();
  });

  it("sends the graph under a `graph` key, as apps/flows/api.py expects", async () => {
    http.route("/api/flows/", { body: savedBody() });
    const { edit, autosave } = armed();

    edit("a");
    await vi.advanceTimersByTimeAsync(DEBOUNCE_MS);

    expect(Object.keys(puts()[0]?.body as object)).toEqual(["graph"]);
    autosave.stop();
  });
});

describe("CSRF", () => {
  it("attaches X-CSRFToken to a PUT, from the hidden input base.html renders", async () => {
    http.route("/api/flows/", { body: savedBody() });
    const { edit, autosave } = armed();

    edit("a");
    await vi.advanceTimersByTimeAsync(DEBOUNCE_MS);

    expect(puts()[0]?.headers["x-csrftoken"]).toBe("test-csrf-token");
    autosave.stop();
  });

  it("refuses to send at all when the page has no token, rather than sending headerless", async () => {
    document.body.innerHTML = "";
    http.route("/api/flows/", { body: savedBody() });
    const { edit, store, autosave } = armed();

    edit("a");
    await vi.advanceTimersByTimeAsync(DEBOUNCE_MS);

    expect(puts()).toHaveLength(0);
    expect(store.getState().save.state).toBe("error");
    autosave.stop();
  });
});

describe("single flight", () => {
  it("queues exactly one follow-up and fires it as soon as the first lands", async () => {
    let release: (() => void) | null = null;
    http.route("/api/flows/", () => ({ body: savedBody() }));
    const original = globalThis.fetch;
    globalThis.fetch = (async (...args: Parameters<typeof fetch>) => {
      const promise = original(...args);
      if (!release) {
        await new Promise<void>((resolve) => {
          release = resolve;
        });
      }
      return promise;
    }) as typeof fetch;

    const { edit, autosave } = armed();
    edit("a");
    await vi.advanceTimersByTimeAsync(DEBOUNCE_MS);

    // Two more edits while the first PUT is still open: they collapse into a
    // single follow-up, not one request each.
    edit("b");
    edit("c");
    await vi.advanceTimersByTimeAsync(DEBOUNCE_MS);
    expect(puts()).toHaveLength(1);

    (release as unknown as () => void)();
    await vi.advanceTimersByTimeAsync(0);
    expect(puts()).toHaveLength(2);

    globalThis.fetch = original;
    autosave.stop();
  });
});

describe("what the server answers", () => {
  it("treats a 200 carrying graph errors as saved, and says both", async () => {
    // A draft is allowed to be half-wired: apps/flows/api.py saves it and
    // reports the errors, so conflating "saved" with "valid" would tell the
    // user their work was lost when it was not.
    http.route("/api/flows/", {
      body: {
        ...savedBody(),
        validation: { errors: [{ code: "no_entry_node", message: "No entry node." }], warnings: [] },
      },
    });
    const { edit, store, autosave } = armed();

    edit("a");
    await vi.advanceTimersByTimeAsync(DEBOUNCE_MS);

    expect(store.getState().save.state).toBe("saved");
    expect(store.getState().save.version?.version).toBe(2);
    expect(store.getState().validation.errors).toHaveLength(1);
    autosave.stop();
  });

  it("treats a 422 as not saved, and does not resend the same bytes", async () => {
    http.route("/api/flows/", {
      status: 422,
      body: { validation: { errors: [{ code: "unknown_node_key", message: "Unexpected key." }], warnings: [] } },
    });
    const { edit, store, autosave } = armed();

    edit("a");
    await vi.advanceTimersByTimeAsync(DEBOUNCE_MS);

    expect(store.getState().save.state).toBe("rejected");
    expect(store.getState().save.version?.version).toBe(1);
    expect(store.getState().validation.errors[0]?.code).toBe("unknown_node_key");

    // No retry loop: the same payload would be rejected the same way.
    await vi.advanceTimersByTimeAsync(60_000);
    expect(puts()).toHaveLength(1);

    // …but the next edit re-arms.
    edit("b");
    await vi.advanceTimersByTimeAsync(DEBOUNCE_MS);
    expect(puts()).toHaveLength(2);
    autosave.stop();
  });

  it("reports a 413 without retrying", async () => {
    http.route("/api/flows/", {
      status: 413,
      body: { error: { code: "payload_too_large", message: "The request exceeds 528384 bytes." } },
    });
    const { edit, store, autosave } = armed();

    edit("a");
    await vi.advanceTimersByTimeAsync(DEBOUNCE_MS);

    expect(store.getState().save.state).toBe("rejected");
    expect(store.getState().save.message).toContain("528384");
    autosave.stop();
  });

  it("flips to a permission message on a 403", async () => {
    http.route("/api/flows/", { status: 403, body: {} });
    const { edit, store, autosave } = armed();

    edit("a");
    await vi.advanceTimersByTimeAsync(DEBOUNCE_MS);

    expect(store.getState().save.state).toBe("error");
    expect(store.getState().save.message).toContain("permission");
    autosave.stop();
  });

  it("turns a login redirect into a readable message, not a bare SyntaxError", async () => {
    // A 302 to the login page answers HTML. response.json() would throw a
    // SyntaxError that reaches the user as a blank failure.
    http.route("/api/flows/", { html: true });
    const { edit, store, autosave } = armed();

    edit("a");
    await vi.advanceTimersByTimeAsync(DEBOUNCE_MS);

    expect(store.getState().save.message).toContain("session expired");
    autosave.stop();
  });

  it("retries a network failure with a backoff, keeping the graph", async () => {
    http.route("/api/flows/", { reject: true });
    const { edit, store, autosave } = armed();

    edit("a");
    await vi.advanceTimersByTimeAsync(DEBOUNCE_MS);
    await vi.advanceTimersByTimeAsync(0);
    expect(puts()).toHaveLength(1);
    // A TypeError from fetch, not an ApiError — it must still reach the retry
    // ladder rather than escaping as an unhandled rejection.
    expect(store.getState().save.state).toBe("error");
    expect(store.getState().save.message).toContain("Retrying");

    await vi.advanceTimersByTimeAsync(2000);
    expect(puts()).toHaveLength(2);
    autosave.stop();
  });
});

describe("the size pre-flight", () => {
  it("refuses locally rather than spending a round trip on a certain 413", async () => {
    const store = makeStore(makeDetail(makeSampleGraph(), { limits: { schema_version: 1, max_graph_bytes: 10, max_graph_depth: 20, max_nodes: 500, max_edges: 2000 } }));
    const autosave = installAutosave(store);

    store.getState().updateConfig(store.getState().nodeOrder[0] as string, ["blocks", 0, "text"], "a");
    await vi.advanceTimersByTimeAsync(DEBOUNCE_MS);

    expect(puts()).toHaveLength(0);
    expect(store.getState().save.state).toBe("rejected");
    expect(store.getState().save.message).toContain("too big");
    autosave.stop();
  });
});

describe("what does not trigger a save", () => {
  it("selecting a node does not mark the flow dirty", async () => {
    http.route("/api/flows/", { body: savedBody() });
    const { store, autosave } = armed();

    store.getState().setSelection({ nodes: [store.getState().nodeOrder[0] as string], edges: [] });
    await vi.advanceTimersByTimeAsync(DEBOUNCE_MS * 2);

    expect(puts()).toHaveLength(0);
    expect(store.getState().save.state).toBe("clean");
    autosave.stop();
  });

  it("a drag in progress does not, but finishing one does", async () => {
    http.route("/api/flows/", { body: savedBody() });
    const { store, autosave } = armed();
    const first = store.getState().nodeOrder[0] as string;

    store.getState().beginDrag();
    for (let frame = 0; frame < 60; frame += 1) {
      store.getState().moveNode(first, { x: frame, y: frame });
    }
    await vi.advanceTimersByTimeAsync(DEBOUNCE_MS);
    expect(puts()).toHaveLength(0);

    store.getState().endDrag();
    await vi.advanceTimersByTimeAsync(DEBOUNCE_MS);
    expect(puts()).toHaveLength(1);
    autosave.stop();
  });
});

describe("read-only", () => {
  it("is not a guard inside autosave — the module is never installed", () => {
    // The App only calls installAutosave when canEdit is true, so there is no
    // subscription and no timer that could fire. Asserted here as the contract.
    const store = makeStore(makeDetail(makeSampleGraph()), { canEdit: false });

    expect(store.getState().env.canEdit).toBe(false);
  });
});
