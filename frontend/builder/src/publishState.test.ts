/**
 * The label table.
 *
 * These are the cases that made the bug survive review: a reload onto an
 * already-published version reaches the toolbar through `load()`, not through
 * the publish handler, so testing publish-by-clicking never covered it.
 */
import { describe, expect, it } from "vitest";

import { publishView } from "./publishState";
import type { SaveSlice } from "./store/store";

const version = (n: number, published: boolean) => ({
  id: `v${n}`,
  version: n,
  published,
  updated_at: "",
});

function save(patch: Partial<SaveSlice> = {}): SaveSlice {
  return { state: "clean", version: null, publishedVersion: null, message: null, issues: [], ...patch };
}

describe("publishView", () => {
  it("reads Live once the server says this version is published", () => {
    const view = publishView(save({ version: version(2, true), publishedVersion: version(2, true) }), "active");

    expect(view.label).toBe("Live · v2");
    expect(view.tone).toBe("success");
    expect(view.publishDisabled).toBe(true);
    expect(view.publishLabel).toBe("Published");
  });

  it("offers Publish again the moment an edit is pending", () => {
    const view = publishView(save({ state: "dirty", version: version(2, true), publishedVersion: version(2, true) }), "active");

    expect(view.publishDisabled).toBe(false);
    expect(view.publishLabel).toBe("Publish");
  });

  it("drops the version number while an edit is in flight, because the next save opens a new one", () => {
    const view = publishView(save({ state: "dirty", version: version(2, true), publishedVersion: version(2, true) }), "active");

    expect(view.label).toBe("Unsaved changes");
    expect(view.label).not.toContain("v2");
    expect(view.liveChip).toBe("v2 live");
  });

  it("still names the draft beside the live one when they differ", () => {
    const view = publishView(save({ version: version(3, false), publishedVersion: version(2, true) }), "active");

    expect(view.label).toBe("No changes · Draft v3");
    expect(view.liveChip).toBe("v2 live");
    expect(view.publishDisabled).toBe(false);
  });

  it("says Archived even when a published version exists, and still offers Publish", () => {
    // services.publish sets status back to ACTIVE, so publishing is how you
    // un-archive. Disabling the button would remove the only route out.
    const view = publishView(save({ version: version(2, true), publishedVersion: version(2, true) }), "archived");

    expect(view.label).toBe("Archived");
    expect(view.tone).toBe("warning");
    expect(view.publishDisabled).toBe(false);
  });

  it("falls back to the save word alone before anything has loaded", () => {
    expect(publishView(save(), undefined).label).toBe("No changes");
  });
});
