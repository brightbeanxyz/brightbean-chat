/** The stats chip's arithmetic (issue #26). */
import { describe, expect, it } from "vitest";

import { chipValues, hasUrlButton } from "./chip";

const STATS = { sent: 10, delivered: 8, failed: 1, clicked: 2 };

const WITH_URL_BUTTON = {
  buttons: [
    {
      id: "b1",
      label: "Docs",
      action: "url",
      url: "https://example.test/docs",
    },
    { id: "b2", label: "Talk", action: "postback" },
  ],
};

describe("hasUrlButton", () => {
  it("finds a url button in the config", () => {
    expect(hasUrlButton(WITH_URL_BUTTON)).toBe(true);
  });

  it("does not treat a postback button as one", () => {
    expect(
      hasUrlButton({
        buttons: [{ id: "b2", label: "Talk", action: "postback" }],
      }),
    ).toBe(false);
  });

  it("finds a card's url_button, which carries no id", () => {
    // The schema's card link is `{label, url}` — apps/flows/schema/nodes.py's
    // `url_button` ref. An id-based test missed every card in the product.
    expect(
      hasUrlButton({
        blocks: [
          {
            type: "card",
            title: "Plan",
            url_button: { label: "Buy", url: "https://x.test" },
          },
        ],
      }),
    ).toBe(true);
  });

  it("finds one on a card inside a gallery", () => {
    expect(
      hasUrlButton({
        blocks: [
          {
            type: "gallery",
            cards: [
              { title: "One" },
              {
                title: "Two",
                url_button: { label: "Buy", url: "https://x.test" },
              },
            ],
          },
        ],
      }),
    ).toBe(true);
  });

  it("does not treat a card with no link as one", () => {
    expect(
      hasUrlButton({
        blocks: [{ type: "card", title: "Plan", subtitle: "No link" }],
      }),
    ).toBe(false);
  });

  it("does not mistake a media block's url for a button", () => {
    // An <img src>, which apps/analytics/tracking.py never wraps. It is told
    // apart by the key that holds it rather than by the absence of an id.
    expect(
      hasUrlButton({
        blocks: [{ type: "image", url: "https://cdn.test/cat.png" }],
      }),
    ).toBe(false);
  });

  it("does not mistake an external_request url for a button", () => {
    expect(hasUrlButton({ method: "POST", url: "https://api.test/hook" })).toBe(
      false,
    );
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

  it("carries failed through, so the card can call it out", () => {
    // The chip briefly dropped `failed` when it gained `clicked`, which left a
    // node whose every send is refused looking identical to a healthy one.
    expect(chipValues(STATS, WITH_URL_BUTTON).failed).toBe(1);
    expect(chipValues({ ...STATS, failed: 0 }, WITH_URL_BUTTON).failed).toBe(0);
  });

  it("computes the click-through rate against sends", () => {
    expect(chipValues(STATS, WITH_URL_BUTTON).ctr).toBe(20);
  });

  it("rounds to one decimal", () => {
    expect(
      chipValues({ ...STATS, sent: 3, clicked: 1 }, WITH_URL_BUTTON).ctr,
    ).toBe(33.3);
  });

  it("shows a zero rate for a card link that nobody clicked", () => {
    // The regression the id-based predicate caused: the backend wraps this link,
    // so "0% of 10" is the truth and suppressing it hid a real number.
    const card = {
      blocks: [
        {
          type: "card",
          title: "Plan",
          url_button: { label: "Buy", url: "https://x.test" },
        },
      ],
    };
    expect(chipValues({ ...STATS, clicked: 0 }, card).ctr).toBe(0);
  });

  it("has no rate for a node with nothing to click", () => {
    // No url button and no recorded click: there is no link here, so a "0.0%"
    // would read as a failure rather than as an absence.
    expect(
      chipValues(
        { ...STATS, clicked: 0 },
        { blocks: [{ type: "text", text: "hi" }] },
      ).ctr,
    ).toBeNull();
  });

  it("has a rate for a node whose only links are in an email body", () => {
    // Nothing in the graph says an authored <a href> is tracked, so recorded
    // clicks are what qualifies the node.
    expect(
      chipValues(
        { ...STATS, clicked: 5 },
        { subject: "Hi", html_body: "<p>x</p>" },
      ).ctr,
    ).toBe(50);
  });

  it("can exceed 100%, because clicks are not deduplicated", () => {
    // SPEC §18 keeps no per-contact history, so one recipient pressing a link
    // three times is three. The label says "clicks per send" for this reason.
    expect(
      chipValues(
        { sent: 5, delivered: 5, failed: 0, clicked: 12 },
        WITH_URL_BUTTON,
      ).ctr,
    ).toBe(240);
  });

  it("never divides by nothing", () => {
    expect(
      chipValues(
        { sent: 0, delivered: 0, failed: 0, clicked: 0 },
        WITH_URL_BUTTON,
      ).ctr,
    ).toBeNull();
  });
});
