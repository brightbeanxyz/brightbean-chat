/**
 * The fetch wrapper. Everything the island sends goes through here.
 *
 * SECURITY-BASELINE §8: CSRF is enforced on every session-authenticated
 * endpoint, the builder data API included, and there is no `csrf_exempt`
 * anywhere in apps/flows/api.py. templates/base.html renders a global
 * `{% csrf_token %}` and hooks htmx's `configRequest` — but that hook covers
 * htmx only, so a React `fetch` has to attach the header itself.
 */
import i18n from "../i18n";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly payload: unknown = null,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/** A non-JSON answer, which in practice is the login page or an HTML 500. */
export class SessionExpiredError extends ApiError {
  constructor(status: number) {
    super(status, "session_expired", i18n.t("apiErrors.sessionExpired"));
    this.name = "SessionExpiredError";
  }
}

export class RequestTimeoutError extends ApiError {
  constructor() {
    super(0, "timeout", i18n.t("apiErrors.requestTimeout"));
    this.name = "RequestTimeoutError";
  }
}

export class MissingCsrfTokenError extends ApiError {
  constructor() {
    super(0, "missing_csrf_token", i18n.t("apiErrors.missingCsrfToken"));
    this.name = "MissingCsrfTokenError";
  }
}

/**
 * Read the token fresh on every mutating request.
 *
 * Django rotates it on login, and the page can outlive a session refresh in
 * another tab. It is one DOM query.
 */
function csrfToken(): string {
  const input = document.querySelector<HTMLInputElement>("input[name=csrfmiddlewaretoken]");
  const token = input?.value;
  if (!token) {
    throw new MissingCsrfTokenError();
  }
  return token;
}

async function readJson(response: Response): Promise<unknown> {
  // Guard the content type before parsing. A 302 to the login page or an HTML
  // error page makes response.json() throw a bare SyntaxError, which surfaces
  // to the user as a blank failure with no idea what to do about it.
  const contentType = response.headers.get("content-type") ?? "";
  if (!contentType.includes("json")) {
    throw new SessionExpiredError(response.status);
  }
  try {
    return await response.json();
  } catch {
    throw new SessionExpiredError(response.status);
  }
}

function errorFrom(status: number, payload: unknown): ApiError {
  const envelope = (payload as { error?: { code?: string; message?: string } } | null)?.error;
  if (envelope?.code) {
    return new ApiError(status, envelope.code, envelope.message ?? envelope.code, payload);
  }
  if (status === 422) {
    return new ApiError(status, "validation_failed", i18n.t("apiErrors.validationFailed"), payload);
  }
  if (status === 403) {
    return new ApiError(status, "forbidden", i18n.t("apiErrors.forbidden"), payload);
  }
  if (status === 404) {
    return new ApiError(status, "not_found", i18n.t("apiErrors.notFound"), payload);
  }
  return new ApiError(status, "server_error", i18n.t("apiErrors.serverError", { status }), payload);
}

/**
 * How long a request may hang before it is abandoned.
 *
 * Without this a proxy that neither answers nor closes leaves the promise
 * pending forever, and because autosave is single-flight that one request
 * stops every later save for the rest of the session — while the indicator
 * still reads "Saving…". A rejection puts the retry ladder back in charge.
 */
export const REQUEST_TIMEOUT_MS = 20_000;

interface RequestOptions {
  method?: "GET" | "PUT" | "POST";
  body?: unknown;
  signal?: AbortSignal;
  timeoutMs?: number;
}

export async function request<T>(url: string, options: RequestOptions = {}): Promise<T> {
  const method = options.method ?? "GET";
  const headers: Record<string, string> = { Accept: "application/json" };

  if (method !== "GET") {
    headers["X-CSRFToken"] = csrfToken();
    headers["Content-Type"] = "application/json";
  }

  const init: RequestInit = { method, headers, credentials: "same-origin" };
  if (options.body !== undefined) {
    init.body = JSON.stringify(options.body);
  }

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), options.timeoutMs ?? REQUEST_TIMEOUT_MS);
  // A caller-supplied signal still wins; this only adds a ceiling.
  options.signal?.addEventListener("abort", () => controller.abort(), { once: true });
  init.signal = controller.signal;

  let response: Response;
  try {
    response = await fetch(url, init);
  } catch (error) {
    // Distinguish "we gave up" from "the network refused", because only the
    // first is worth telling the user we are still retrying.
    if (controller.signal.aborted && !options.signal?.aborted) {
      throw new RequestTimeoutError();
    }
    throw error;
  } finally {
    clearTimeout(timeout);
  }

  const payload = await readJson(response);

  if (!response.ok) {
    throw errorFrom(response.status, payload);
  }
  return payload as T;
}
