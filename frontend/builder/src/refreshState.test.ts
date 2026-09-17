import { describe, expect, it } from "vitest";

import { refreshApplies } from "./refreshState";

describe("refreshApplies", () => {
  it("applies to an untouched store", () => {
    expect(refreshApplies("clean", 3, 3)).toBe(true);
  });

  it("applies after an autosave, which leaves the store saved rather than clean", () => {
    // The regression. Gating on "clean" alone meant the first autosave of a
    // session stopped every later refresh from updating validation or versions.
    expect(refreshApplies("saved", 7, 7)).toBe(true);
  });

  it("does not apply when an edit raced the request", () => {
    expect(refreshApplies("clean", 3, 4)).toBe(false);
    expect(refreshApplies("saved", 3, 4)).toBe(false);
  });

  it("does not apply while the server has not seen the graph", () => {
    // Nothing here has been saved, so any verdict describes an older graph —
    // even though the revision has not moved since the request started.
    for (const state of ["dirty", "saving", "rejected", "error"] as const) {
      expect(refreshApplies(state, 5, 5)).toBe(false);
    }
  });
});
