/**
 * The media picker round trip, against the contract
 * apps/media_library/picker.py's docstring states.
 *
 * The assertion that matters most is that choosing an asset stores its `id` and
 * not its `url`: the picker mints that URL per request, and a block holding one
 * stops working the moment the storage backend changes — which is the entire
 * reason the library exists.
 */
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { installCsrfToken, stubHttp, type HttpStub } from "../test/http";
import { makeDetail } from "../test/fixtures";
import { makeStore, renderWith } from "../test/render";
import { sampleConfig } from "../schema/sample";
import { Inspector } from "./Inspector";

let http: HttpStub;

/**
 * Generous, because the picker debounces its search by 250 ms and Testing
 * Library's default findBy timeout is 1 s — close enough that a loaded CI
 * runner turns a correct test into an intermittent one.
 */
const SETTLE = { timeout: 5000 };

const asset = (id: string, overrides: Record<string, unknown> = {}) => ({
  id,
  kind: "image",
  mime: "image/png",
  filename: `${id}.png`,
  title: `Asset ${id}`,
  alt_text: "",
  size: 1024,
  width: 800,
  height: 600,
  folder_id: null,
  url: `https://host/m/token-${id}/`,
  thumbnail_url: `https://host/m/token-${id}/thumb/`,
  created_at: "2026-08-22T10:00:00+00:00",
  platform_warnings: [],
  ...overrides,
});

beforeEach(() => {
  http = stubHttp();
  installCsrfToken();
});

afterEach(() => http.restore());

function openMessageWithImageBlock() {
  const config = sampleConfig("send_message") as { blocks: Record<string, unknown>[] };
  config.blocks[0] = { type: "image", url: "https://example.com/image.png" };
  const store = makeStore(
    makeDetail({
      schema: 1,
      nodes: [{ id: "n1", type: "send_message", position: { x: 0, y: 0 }, config }],
      edges: [],
    }),
  );
  store.getState().setSelection({ nodes: ["n1"], edges: [] });
  renderWith(store, <Inspector />);
  return store;
}

const pickerRequests = () => http.requests.filter((request) => request.url.includes("/media/picker/"));

describe("the picker", () => {
  it("stores the asset id and clears the URL, never the per-request delivery URL", async () => {
    http.route("/media/picker/", { body: { results: [asset("a1")], folders: [], next_cursor: null } });
    const store = openMessageWithImageBlock();

    fireEvent.click(screen.getByRole("button", { name: /choose from the library/i }));
    fireEvent.click(await screen.findByRole("button", { name: /Asset a1/ }, SETTLE));

    const block = (store.getState().config["n1"] as { blocks: Record<string, unknown>[] }).blocks[0];
    expect(block).toHaveProperty("media_id", "a1");
    expect(block).not.toHaveProperty("url");
    expect(JSON.stringify(block)).not.toContain("token-a1");
  });

  it("clears the asset id when a URL is typed instead, so the anyOf is never both", async () => {
    http.route("/media/picker/", { body: { results: [asset("a1")], folders: [], next_cursor: null } });
    const store = openMessageWithImageBlock();

    fireEvent.click(screen.getByRole("button", { name: /choose from the library/i }));
    fireEvent.click(await screen.findByRole("button", { name: /Asset a1/ }, SETTLE));
    fireEvent.click(screen.getByRole("button", { name: /remove the chosen library asset/i }));

    fireEvent.change(screen.getByLabelText(/direct url/i), { target: { value: "https://example.com/a.png" } });

    const block = (store.getState().config["n1"] as { blocks: Record<string, unknown>[] }).blocks[0];
    expect(block).toHaveProperty("url", "https://example.com/a.png");
    expect(block).not.toHaveProperty("media_id");
  });

  it("passes the search term, the folder and the kind as the documented query params", async () => {
    http.route("/media/picker/", { body: { results: [], folders: [{ id: "f1", name: "Brand", parent_id: null }], next_cursor: null } });
    openMessageWithImageBlock();

    fireEvent.click(screen.getByRole("button", { name: /choose from the library/i }));
    await waitFor(() => expect(pickerRequests().length).toBeGreaterThan(0), SETTLE);

    // The kind comes from the block's own type, so the list is pre-filtered.
    expect(pickerRequests()[0]?.url).toContain("kind=image");

    fireEvent.change(screen.getByLabelText("Search the library"), { target: { value: "logo" } });
    await waitFor(() => expect(pickerRequests().some((request) => request.url.includes("q=logo"))).toBe(true), SETTLE);

    fireEvent.change(await screen.findByLabelText("Folder", {}, SETTLE), { target: { value: "f1" } });
    await waitFor(() => expect(pickerRequests().some((request) => request.url.includes("folder=f1"))).toBe(true), SETTLE);
  });

  it("pages with the opaque cursor rather than an offset", async () => {
    http.route("/media/picker/", (request) =>
      request.url.includes("cursor=")
        ? { body: { results: [asset("a2")], folders: [], next_cursor: null } }
        : { body: { results: [asset("a1")], folders: [], next_cursor: "opaque-cursor" } },
    );
    openMessageWithImageBlock();

    fireEvent.click(screen.getByRole("button", { name: /choose from the library/i }));
    fireEvent.click(await screen.findByRole("button", { name: /load more/i }, SETTLE));

    expect(await screen.findByRole("button", { name: /Asset a2/ }, SETTLE)).toBeInTheDocument();
    expect(pickerRequests().some((request) => request.url.includes("cursor=opaque-cursor"))).toBe(true);
  });

  it("shows platform warnings beside an asset without disabling it", async () => {
    // "It never means 'cannot attach'; the target platform is not fixed until
    // send time" — apps/media_library/picker.py.
    http.route("/media/picker/", {
      body: {
        results: [asset("a1", { platform_warnings: ["WhatsApp accepts image files up to 5 MB; this one is 8.0 MB."] })],
        folders: [],
        next_cursor: null,
      },
    });
    openMessageWithImageBlock();

    fireEvent.click(screen.getByRole("button", { name: /choose from the library/i }));
    const choice = await screen.findByRole("button", { name: /Asset a1/ }, SETTLE);

    expect(choice).not.toBeDisabled();
    expect(choice.textContent).toContain("up to 5 MB");
  });

  it("restarts at the top when a stale folder id answers 404", async () => {
    http.route("/media/picker/", (request) =>
      request.url.includes("folder=stale")
        ? { status: 404, body: {} }
        : { body: { results: [asset("a1")], folders: [{ id: "stale", name: "Gone", parent_id: null }], next_cursor: null } },
    );
    openMessageWithImageBlock();

    fireEvent.click(screen.getByRole("button", { name: /choose from the library/i }));
    fireEvent.change(await screen.findByLabelText("Folder", {}, SETTLE), { target: { value: "stale" } });

    // Not an error the user can act on — the folder simply no longer exists.
    expect(await screen.findByRole("button", { name: /Asset a1/ }, SETTLE)).toBeInTheDocument();
    expect(screen.queryByText(/could not be reached/i)).toBeNull();
  });

  it("reads the picker with no CSRF header, because it is a GET", async () => {
    http.route("/media/picker/", { body: { results: [], folders: [], next_cursor: null } });
    openMessageWithImageBlock();

    fireEvent.click(screen.getByRole("button", { name: /choose from the library/i }));
    await waitFor(() => expect(pickerRequests().length).toBeGreaterThan(0), SETTLE);

    expect(pickerRequests()[0]?.method).toBe("GET");
    expect(pickerRequests()[0]?.headers["x-csrftoken"]).toBeUndefined();
  });
});
