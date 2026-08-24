# BrightBean Chat — Security Baseline

This is the per-PR security checklist for every implementation issue. **Every PR must satisfy the applicable items below**; reviewers verify them, CI enforces the automatable ones, and a security review runs over each layer's merged diff at the layer gate (see `docs/ROADMAP.md` → Execution model). The final Layer-7 security pass (issue #29) verifies traceability: every item here maps to at least one test.

Threat model in one paragraph: a self-hosted deployment exposes webhook endpoints and public token routes to the internet from day one; message content, usernames, comment bodies, and media URLs arrive from **strangers** (attacker-controlled); users author automations that make server-side HTTP requests; multiple workspaces share one database; the encrypted platform credentials are the crown jewels.

## 1. Tenancy isolation
- Every queryset on tenant data goes through the workspace-scoped base manager (from L1-A). No `.objects.all()` on tenant models in views/APIs.
- Cross-workspace access to any object returns **404** (never 403 — no existence oracle).
- Any PR that adds views or API endpoints must extend the IDOR fuzz suite (helper from L1-A): every new endpoint is hit as an authenticated user of a *different* workspace and must 404.

## 2. Untrusted inbound content
- All platform-delivered content — message text, usernames, profile fields, comment bodies, attachment/media URLs — is attacker-controlled. Escape on render, never `mark_safe`, never render platform-supplied HTML raw.
- Webhook payload parsing is defensive: type-check every field, tolerate missing/extra keys, cap sizes. Fixture suites must include malformed and hostile payloads (oversized, wrong types, script/injection strings in every string field).

## 3. Templating (SSTI ban)
- `{{placeholder}}` rendering is **plain token substitution** via the one shared renderer (lives in the flow engine, `flows/rendering.py`). User- or contact-supplied content is **never** evaluated by Django/Jinja template engines.
- In HTML contexts (email bodies), substituted values are HTML-escaped; elsewhere plain text. Variables sourced from External Request responses are untrusted like contact input.

## 4. Public token routes
- All unauthenticated token routes — unsubscribe `/u/`, media delivery `/m/`, click `/c/`, pixel `/o/`, flow-preview links, `/internal/tick` — use the one shared signing utility (Django signer, versioned payloads, expiry where the use case allows).
- Verification is constant-time; any failure returns a generic 404 (no error detail, no timing oracle).
- **Two documented divergences**, each argued where it lives and named here so the checklist does not read as unmet. `/internal/tick` uses a bare env token compared with `constant_time_compare` (`apps/queueing/views.py`): a third-party pinger stores one URL forever, so `max_age=None` would make a signed token "a bare token with extra steps". Telegram flow-preview links use a stored HMAC digest of a random handle (`apps/channels/models.py`), because Telegram caps a `start` payload at 64 characters, which a signed payload does not fit. Both keep the properties that matter — constant-time compare, bare 404, no oracle.
- A token route's path prefix goes in `apps/common/logging.py`'s `_TOKEN_PATH_PREFIXES` so the credential is scrubbed out of request lines. `tests/test_token_routes.py` derives the list from the URL conf and fails when a route is missing from it.

## 5. Secrets
- Credentials and tokens are stored only in encrypted fields (AES-256-GCM util from L1-A). Never in plain columns, fixtures, or logs.
- The global log-scrubbing filter (L1-A) is installed in all environments; tests assert tokens never appear in captured logs, error reports, admin list displays, or API responses.

## 6. Outbound HTTP (SSRF)
- Any server-initiated request to a user-supplied or contact-supplied URL goes through the SSRF guard — `apps.common.outbound.guarded_request`, landed by issue #15 — for the External Request node, outbound webhook deliveries, media fetch-by-URL and provider callbacks. No exceptions; new call sites add a test proving the guard is in the path.
- "Proving" means `tests/ssrf.py`'s `guard_required()`, which fails any HTTP request made inside its block that did not come from the guard. Asserting that a patched `guarded_request` was called is not the same claim: it stays green when a second, unguarded request is made beside it.
- The guard denies loopback, link-local (cloud metadata), multicast, reserved and unspecified addresses and the deployment's own host, resolves before connecting, **pins the resolved address** so DNS cannot rebind between the check and the connect, re-validates every redirect (cap 3), allows only `http`/`https`, and caps the response body with a streaming cutoff — requesting `Accept-Encoding: identity` and declining to expand a body compressed anyway, since a cap on a decompressed stream fires only after the allocation it exists to prevent. `EXTERNAL_REQUEST_ALLOW_PRIVATE` relaxes the private-range rule alone, for on-prem deployments; it opens nothing else.
- `apps.channels.providers.base.request_json` is the sibling for URLs an adapter builds from constants and stored ids. A call site that cannot tell which of the two it is wants the guard.
- **Two paths reach the network without passing either**, because neither is HTTP-over-httpx: Django's SMTP backend to a workspace-configured host, and boto3 to SES (both in `apps/channels/providers/email_backends.py`). SMTP applies the guard's own address rules as a pre-flight (`refusal_for`/`resolve_host`) but does not pin the connection; boto3 owns its transport entirely. `tests/test_ssrf_call_sites.py` counts the doors and pins this exception so a third cannot appear quietly.

## 7. Input limits
- Request body-size caps on every webhook and public endpoint, enforced before signature work where possible and always before DB writes.
- JSON documents authored by users (`graph_json`, `filter_json`, `config_json`, import files) get size + depth caps and schema validation that **rejects unknown keys** (mass-assignment guard).
- Set-wise condition evaluation compiles through the ORM only, with a field/operator allowlist — no string-built SQL anywhere.

## 8. Web platform hardening
- CSP with per-request nonces (Studio pattern) on every page, including the React builder island.
- Session/CSRF cookies `Secure`, `HttpOnly` (session), `SameSite=Lax`; CSRF enforced on all session-authenticated endpoints including the builder data API.
- Auth endpoints (login, signup, password reset) rate-limited; responses enumeration-safe.
- Production settings refuse to boot without `SECRET_KEY` + `ENCRYPTION_KEY_SALT`; `DEBUG` off outside dev; security headers (HSTS with preload and subdomains, `X-Content-Type-Options: nosniff`, frame-deny, referrer-policy) set by **the application**, in `config/settings/production.py`, and asserted by `apps/common/tests/test_checks.py`. Setting them at the proxy as well is belt-and-braces and belongs with the deployment work (issue #28); this line previously attributed them to a Caddy config that does not exist in the repository, which understated the posture rather than overstating it — but a checklist that names the wrong enforcement point cannot be verified against the code.

## 9. File uploads (media library)
- Content-type determined by sniffing, not extension. SVG/HTML/unknown types are served with `Content-Disposition: attachment` + `nosniff` (stored-XSS ban); only safe image types render inline.
- Per-file and per-workspace size quotas. Delivery URLs signed and unguessable.

## 10. Supply chain
- `requirements.txt`/lockfile and `package-lock.json` pinned; `pip-audit` and `npm audit` run in CI (build fails on known-vulnerable deps without a documented waiver); Dependabot enabled; Bandit (or ruff security rules) in lint.

## 11. Gate policy
- **Per PR**: applicable checklist items above + tests. Security-critical issues (webhook framework #4, External Request #15, media library #16, public API #25, and every channel adapter) additionally get a dedicated security review at PR time.
- **Per layer gate**: security review over the layer's merged diff, IDOR fuzz suite green, dependency audits clean (or waivers documented in the PR).
- **Traceability**: [`docs/security-audit.md`](security-audit.md) maps every bullet above, and every SPEC §19 bullet, to the test that enforces it. `tests/test_security_audit.py` fails when a bullet is added here without being mapped, or when a mapped test stops existing.
