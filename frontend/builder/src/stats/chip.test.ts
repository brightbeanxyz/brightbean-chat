/** The stats chip's arithmetic (issue #26). */
import { describe, expect, it } from "vitest";

import { chipValues, hasUrlButton } from "./chip";

const STATS = { sent: 10, delivered: 8, failed: 1, clicked: 2 };

const WITH_URL_BUTTON = {
  buttons: [
    { id: "b1", label: "Docs", action: "url", url: "https://example.test/docs" },
    { id: "b2", label: "Talk", action: "postback" },
  ],
};

describe("hasUrlButton", () => {
  it("finds a url button in the config", () => {
    expect(hasUrlButton(WITH_URL_BUTTON)).toBe(true);
  });

  it("does not treat a postback button as one", () => {
    expect(hasUrlButton({ buttons: [{ id: "b2", label: "Talk", action: "postback" }] })).toBe(false);
  });

  it("finds one nested inside a card", () => {
    expect(
      hasUrlButton({
        blocks: [{ type: "card", title: "Plan", buttons: [{ id: "b1", label: "Buy", url: "https://x.test" }] }],
      }),
    ).toBe(true);
  });

  it("does not mistake a media block's url for a button", () => {
    // A media url has no `id` beside it — it is an <img src>, and
    // apps/analytics/tracking.py never wraps one.
    expect(hasUrlButton({ blocks: [{ type: "image", url: "https://cdn.test/cat.png" }] })).toBe(false);
  });

  it("tolerates anything a hand-edited graph might hold", () => {
    expect(hasUrlButton(null)).toBe(false);
    expect(hasUrlButton("nope")).toBe(false);
    expect(hasUrlButton([1, 2, 3])).toBe(false);
  });
});

describe("chipValues", () => {
  it("shows sent, delivered and clicked", () => {
    const chip = chipValues(STATS, WITH_URL_BUTTON);
    expect([chip.sent, chip.delivered, chip.clicked]).toEqual([10, 8, 2]);
  });

  it("computes the click-through rate against sends", () => {
    expect(chipValues(STATS, WITH_URL_BUTTON).ctr).toBe(20);
  });

  it("rounds to one decimal", () => {
    expect(chipValues({ ...STATS, sent: 3, clicked: 1 }, WITH_URL_BUTTON).ctr).toBe(33.3);
  });

  it("has no rate for a node with nothing to click", () => {
    // No url button and no recorded click: there is no link here, so a "0.0%"
    // would read as a failure rather than as an absence.
    expect(chipValues({ ...STATS, clicked: 0 }, { blocks: [{ type: "text", text: "hi" }] }).ctr).toBeNull();
  });

  it("has a rate for a node whose only links are in an email body", () => {
    // Nothing in the graph says an authored <a href> is tracked, so recorded
    // clicks are what qualifies the node.
    expect(chipValues({ ...STATS, clicked: 5 }, { subject: "Hi", html_body: "<p>x</p>" }).ctr).toBe(50);
  });

  it("never divides by nothing", () => {
    expect(chipValues({ sent: 0, delivered: 0, failed: 0, clicked: 0 }, WITH_URL_BUTTON).ctr).toBeNull();
  });
});
