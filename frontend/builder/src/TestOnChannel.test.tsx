/**
 * The preview button.
 *
 * The empty states are the cases worth pinning. The endpoint answers 200 with
 * `ok: false` for both of them — nothing connected, and a channel with no live
 * test at all — precisely so this component can explain rather than fail; a 4xx
 * would take the same journey as a server error and show "The server answered
 * 400."
 */
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { TestOnChannel } from "./TestOnChannel";
import { installCsrfToken, stubHttp, type HttpStub } from "./test/http";
import { makeStore, renderWith } from "./test/render";

let http: HttpStub;

beforeEach(() => {
  http = stubHttp();
  installCsrfToken();
});

afterEach(() => http.restore());

const SETTLE = { timeout: 5000 };

describe("the preview button", () => {
  it("asks the server for a link and shows it", async () => {
    http.route("/preview/", {
      body: {
        ok: true,
        deep_link: "https://t.me/acme_bot?start=preview-abc",
        platform: "telegram",
        platform_label: "Telegram",
        account: "@acme_bot",
        instructions: "",
        expires_in: 900,
      },
    });

    renderWith(makeStore(), <TestOnChannel />);
    fireEvent.click(screen.getByRole("button", { name: "Test this flow" }));

    const link = await screen.findByRole("link", { name: /Open @acme_bot/ }, SETTLE);
    expect(link).toHaveAttribute("href", "https://t.me/acme_bot?start=preview-abc");
    // Leaving the app in a new tab: without this the opened tab can reach back
    // through window.opener.
    expect(link).toHaveAttribute("rel", "noopener noreferrer");

    const [request] = http.requests;
    expect(request?.method).toBe("POST");
    expect(request?.headers["x-csrftoken"]).toBeTruthy();
  });

  it("mints a fresh link on every press rather than caching one", async () => {
    let issued = 0;
    http.route("/preview/", () => ({
      body: {
        ok: true,
        deep_link: `https://t.me/acme_bot?start=preview-${++issued}`,
        platform: "telegram",
        platform_label: "Telegram",
        account: "@acme_bot",
        instructions: "",
        expires_in: 900,
      },
    }));

    renderWith(makeStore(), <TestOnChannel />);
    const button = screen.getByRole("button", { name: "Test this flow" });

    fireEvent.click(button);
    await screen.findByRole("link", {}, SETTLE);
    fireEvent.click(button);

    // A link expires in minutes, so one held over from an hour ago is a link
    // that no longer works.
    await waitFor(
      () => expect(screen.getByRole("link")).toHaveAttribute("href", "https://t.me/acme_bot?start=preview-2"),
      SETTLE,
    );
  });

  it("explains the empty state instead of failing", async () => {
    http.route("/preview/", {
      body: {
        ok: false,
        reason: "no_connection",
        message: "Connect a Telegram bot first.",
        settings_url: "/w/ws/settings/channels/telegram/connect/",
      },
    });

    renderWith(makeStore(), <TestOnChannel />);
    fireEvent.click(screen.getByRole("button", { name: "Test this flow" }));

    expect(await screen.findByText(/Connect a Telegram bot first/, {}, SETTLE)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Connect one" })).toHaveAttribute(
      "href",
      "/w/ws/settings/channels/telegram/connect/",
    );
    expect(screen.queryByRole("link", { name: /Open/ })).toBeNull();
  });

  it("names the channel the flow is actually built for", async () => {
    // An Instagram automation whose only preview said "Test on Telegram" could
    // not be seen before it went to real customers.
    http.route("/preview/", {
      body: {
        ok: true,
        deep_link: "https://ig.me/m/acme?ref=preview-abc",
        platform: "instagram",
        platform_label: "Instagram",
        account: "@acme",
        instructions: "Send any message once the chat opens — that first message is what starts the test.",
        expires_in: 900,
      },
    });

    renderWith(makeStore(), <TestOnChannel />);
    fireEvent.click(screen.getByRole("button", { name: "Test this flow" }));

    expect(await screen.findByRole("link", { name: /Open @acme on Instagram/ }, SETTLE)).toBeInTheDocument();
    // Meta opens a composer rather than sending anything, so the tester has to
    // be told. Without this, a working link gets reported as broken.
    expect(screen.getByText(/Send any message once the chat opens/)).toBeInTheDocument();
  });

  it("explains a channel that has no live test, without offering somewhere to go", async () => {
    http.route("/preview/", {
      body: {
        ok: false,
        reason: "unsupported_platform",
        message: "There is no live test on WhatsApp.",
      },
    });

    renderWith(makeStore(), <TestOnChannel />);
    fireEvent.click(screen.getByRole("button", { name: "Test this flow" }));

    expect(await screen.findByText(/no live test on WhatsApp/, {}, SETTLE)).toBeInTheDocument();
    // Nothing to connect would change the answer, so there is no link to offer.
    expect(screen.queryByRole("link")).toBeNull();
  });

  it("reports a server failure without offering a broken link", async () => {
    http.route("/preview/", { status: 500, body: {} });

    renderWith(makeStore(), <TestOnChannel />);
    fireEvent.click(screen.getByRole("button", { name: "Test this flow" }));

    expect(await screen.findByText(/The server answered 500/, {}, SETTLE)).toBeInTheDocument();
    expect(screen.queryByRole("link")).toBeNull();
  });
});
