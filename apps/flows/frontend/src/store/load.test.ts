/**
 * What survives `load()`.
 *
 * `detail.flow` used to be dropped on the floor here, which is why nothing in
 * the island could tell a live flow from a draft: the API had been sending
 * `flow.status` all along and the store threw it away.
 */
import { describe, expect, it } from "vitest";

import { makeDetail, makeSampleGraph } from "../test/fixtures";
import { makeStore } from "../test/render";

describe("load", () => {
  it("keeps the flow meta the detail response carries", () => {
    const store = makeStore(
      makeDetail(makeSampleGraph(), {
        flow: { id: "flow-1", name: "Welcome", status: "active", folder: "", updated_at: "" },
      }),
    );

    expect(store.getState().flow?.status).toBe("active");
  });

  it("keeps the published version, not just the current one", () => {
    const store = makeStore(
      makeDetail(makeSampleGraph(), {
        version: { id: "v3", version: 3, published: false, updated_at: "" },
        published_version: { id: "v2", version: 2, published: true, updated_at: "" },
      }),
    );

    expect(store.getState().save.version?.version).toBe(3);
    expect(store.getState().save.publishedVersion?.version).toBe(2);
  });

  it("starts with no flow meta before anything has loaded", () => {
    expect(makeStore(null).getState().flow).toBeNull();
  });
});
