/**
 * A recording `fetch` stub.
 *
 * Deliberately not msw: the assertions here are about headers, status codes and
 * request bodies, which a stub covers exactly as well, and msw would add a
 * large dependency tree to a lockfile that has to keep `npm audit
 * --audit-level=low` green with no waiver mechanism.
 */
import { vi } from "vitest";

export interface RecordedRequest {
  url: string;
  method: string;
  headers: Record<string, string>;
  body: unknown;
}

export interface StubbedResponse {
  status?: number;
  body?: unknown;
  /** Simulates a login redirect or an HTML error page. */
  html?: boolean;
  reject?: boolean;
}

export type Route = (request: RecordedRequest) => StubbedResponse | undefined;

export interface HttpStub {
  requests: RecordedRequest[];
  route: (matcher: RegExp | string, response: StubbedResponse | Route) => void;
  restore: () => void;
}

export function stubHttp(): HttpStub {
  const requests: RecordedRequest[] = [];
  const routes: { matcher: RegExp | string; response: StubbedResponse | Route }[] = [];

  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = String(input);
    const headers = Object.fromEntries(
      Object.entries((init?.headers ?? {}) as Record<string, string>).map(([key, value]) => [key.toLowerCase(), value]),
    );
    const recorded: RecordedRequest = {
      url,
      method: init?.method ?? "GET",
      headers,
      body: typeof init?.body === "string" ? JSON.parse(init.body) : undefined,
    };
    requests.push(recorded);

    const hit = routes
      .filter(({ matcher }) => (typeof matcher === "string" ? url.includes(matcher) : matcher.test(url)))
      .pop();
    const stubbed = typeof hit?.response === "function" ? hit.response(recorded) : hit?.response;

    if (stubbed?.reject) {
      throw new TypeError("Network request failed");
    }
    const status = stubbed?.status ?? 200;
    const contentType = stubbed?.html ? "text/html" : "application/json";
    const payload = stubbed?.html ? "<html>login</html>" : JSON.stringify(stubbed?.body ?? {});

    return new Response(payload, { status, headers: { "content-type": contentType } });
  });

  const original = globalThis.fetch;
  globalThis.fetch = fetchMock as unknown as typeof fetch;

  return {
    requests,
    route: (matcher, response) => routes.push({ matcher, response }),
    restore: () => {
      globalThis.fetch = original;
    },
  };
}

/** The hidden input templates/base.html renders at the top of <body>. */
export function installCsrfToken(value = "test-csrf-token"): void {
  const input = document.createElement("input");
  input.setAttribute("name", "csrfmiddlewaretoken");
  input.setAttribute("type", "hidden");
  input.value = value;
  document.body.appendChild(input);
}
