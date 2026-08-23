/**
 * SPEC §16's preview button.
 *
 * The empty state is the case worth pinning. The endpoint answers 200 with
 * `ok: false` when the workspace has no Telegram bot, precisely so this
 * component can explain rather than fail — a 4xx would take the same journey as
 * a server error and show "The server answered 400."
 */
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { TestOnTelegram } from "./TestOnTelegram";
import { installCsrfToken, stubHttp, type HttpStub } from "./test/http";
import { makeStore, renderWith } from "./test/render";

let http: HttpStub;

beforeEach(() => {
  http = stubHttp();
  installCsrfToken();
});

afterEach(() => http.restore());

const SETTLE = { timeout: 5000 };

describe("Test on Telegram", () => {
  it("asks the server for a link and shows it", async () => {
    http.route("/preview/", {
      body: { ok: true, deep_link: "https://t.me/acme_bot?start=preview-abc", bot: "@acme_bot", expires_in: 900 },
    });

    renderWith(makeStore(), <TestOnTelegram />);
    fireEvent.click(screen.getByRole("button", { name: "Test on Telegram" }));

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
      body: { ok: true, deep_link: `https://t.me/acme_bot?start=preview-${++issued}`, bot: "@acme_bot", expires_in: 900 },
    }));

    renderWith(makeStore(), <TestOnTelegram />);
    const button = screen.getByRole("button", { name: "Test on Telegram" });

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

    renderWith(makeStore(), <TestOnTelegram />);
    fireEvent.click(screen.getByRole("button", { name: "Test on Telegram" }));

    expect(await screen.findByText(/Connect a Telegram bot first/, {}, SETTLE)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Connect Telegram" })).toHaveAttribute(
      "href",
      "/w/ws/settings/channels/telegram/connect/",
    );
    expect(screen.queryByRole("link", { name: /Open/ })).toBeNull();
  });

  it("reports a server failure without offering a broken link", async () => {
    http.route("/preview/", { status: 500, body: {} });

    renderWith(makeStore(), <TestOnTelegram />);
    fireEvent.click(screen.getByRole("button", { name: "Test on Telegram" }));

    expect(await screen.findByText(/The server answered 500/, {}, SETTLE)).toBeInTheDocument();
    expect(screen.queryByRole("link")).toBeNull();
  });
});
