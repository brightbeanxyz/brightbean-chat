/** Toolbar labels keep publication and trigger reachability separate from saves. */
import { describe, expect, it } from "vitest";

import { publishView } from "./publishState";
import type { SaveSlice } from "./store/store";

const version = (n: number, published: boolean, published_at: string | null = published ? "2026-10-01" : null) => ({
  id: `v${n}`,
  version: n,
  published,
  published_at,
  updated_at: "",
});

function save(patch: Partial<SaveSlice> = {}): SaveSlice {
  return { state: "clean", version: null, publishedVersion: null, message: null, issues: [], ...patch };
}

describe("publishView", () => {
  const published = save({ version: version(2, true), publishedVersion: version(2, true) });

  it("offers Set offline on a published flow with nothing new to publish", () => {
    const view = publishView(published, "active", 2, 1);

    expect(view.statusLabel).toBe("Published");
    expect(view.action).toBe("offline");
    expect(view.publishLabel).toBe("Set offline");
    expect(view.publishHint).toBe("Stops this flow now, including conversations already in it.");
    expect(view.secondary).toBeNull();
  });

  it("calls out a published flow whose triggers are all off and offers to turn them on", () => {
    const view = publishView(published, "active", 2, 0);

    expect(view.statusLabel).toBe("Published · Triggers off");
    expect(view.action).toBe("enable");
    expect(view.publishLabel).toBe("Turn on triggers");
    // Still live for the API, sequences and other flows, so it still needs a way out.
    expect(view.secondary).toBe("offline");
  });

  it("calls out a published flow with no triggers", () => {
    const view = publishView(published, "active", 0, 0);

    expect(view.statusLabel).toBe("Published · No triggers");
    // "All off" needs at least one trigger, so there is nothing to turn on.
    expect(view.action).toBe("offline");
  });

  it("offers Set live again as soon as an edit is pending", () => {
    const view = publishView(save({ ...published, state: "dirty" }), "active", 2, 1);

    expect(view.action).toBe("publish");
    expect(view.publishLabel).toBe("Set live");
    expect(view.publishHint).toBeNull();
    expect(view.saveLabel).toBe("Unsaved changes");
    // The flow is still live while the edit waits, so it can still be switched off.
    expect(view.secondary).toBe("offline");
  });

  it("offers Set live first and Set offline beside it for a saved draft of a live flow", () => {
    // A draft that does not validate must not stand between a live flow and
    // switching it off.
    const view = publishView(save({ version: version(3, false), publishedVersion: version(2, true) }), "active", 2, 1);

    expect(view.action).toBe("publish");
    expect(view.publishLabel).toBe("Set live");
    expect(view.secondary).toBe("offline");
  });

  it("names a saved draft while still showing the published status", () => {
    const view = publishView(save({ version: version(3, false), publishedVersion: version(2, true) }), "active", 2, 0);

    expect(view.statusLabel).toBe("Published · Triggers off");
    expect(view.saveLabel).toBe("No changes · Draft v3");
  });

  it("says Archived even when a published version exists", () => {
    const view = publishView(published, "archived", 2, 1);

    expect(view.statusLabel).toBe("Archived");
    expect(view.action).toBe("publish");
  });

  it("says Offline and offers Set live, without calling its last live version a draft", () => {
    const view = publishView(save({ version: version(2, false, "2026-10-01"), publishedVersion: null }), "offline", 2, 1);

    expect(view.statusLabel).toBe("Offline");
    expect(view.statusTone).toBe("warning");
    expect(view.action).toBe("publish");
    expect(view.publishLabel).toBe("Set live");
    expect(view.secondary).toBeNull();
    expect(view.saveLabel).toBe("No changes");
  });

  it("still names an edit made while offline, because Set live will publish it", () => {
    const view = publishView(save({ version: version(3, false), publishedVersion: null }), "offline", 2, 1);

    expect(view.saveLabel).toBe("No changes · Draft v3");
  });

  it("uses Draft before publication", () => {
    const view = publishView(save(), "draft");

    expect(view.secondary).toBeNull();
    expect(view.statusLabel).toBe("Draft");
    expect(view.saveLabel).toBe("No changes");
  });
});
