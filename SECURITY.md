# Security policy

BrightBean Chat is self-hosted software that speaks to the public internet from
day one: it exposes webhook endpoints and unauthenticated token routes, it
handles message content written by strangers, and it stores encrypted platform
credentials for every channel a workspace connects. We take reports about any of
that seriously.

## Reporting a vulnerability

**Please do not open a public issue.**

Use GitHub's private vulnerability reporting:
[**Report a vulnerability**](https://github.com/brightbeanxyz/brightbean-chat/security/advisories/new).
That opens a private advisory visible only to you and the maintainers, with a
place to discuss a fix and to coordinate disclosure.

If that is not available to you, open a regular issue saying only that you have
a security report and asking for a private channel — no details.

Helpful reports include:

- what an attacker can do, and what they need in order to do it (a valid
  account? another workspace's id? nothing at all?)
- the affected version or commit
- reproduction steps, ideally against a local `docker compose up`
- any proof-of-concept request, payload or flow export

### What to expect

| | |
|---|---|
| Acknowledgement | within 3 working days |
| Initial assessment | within 7 working days |
| Fix or mitigation for a confirmed high-severity issue | as fast as we can, with a target of 30 days |
| Credit | in the advisory, unless you prefer otherwise |

We will keep you updated while a fix is in progress, and we will tell you when
it ships and under what advisory.

## Supported versions

This project is pre-1.0. Only the current `main` branch is supported: fixes land
there, and self-hosters upgrade by pulling and rebuilding
([`docs/self-hosting.md`](docs/self-hosting.md) § Upgrades). There are no
backports to older commits.

## Scope

**In scope** — anything in this repository, including:

- cross-workspace or cross-organization data access (a workspace reading, writing
  or learning of another's data)
- authentication and session handling, including the invitation and
  password-reset flows
- webhook signature verification, replay handling, and the public token routes
  (`/u/`, `/c/`, `/o/`, media delivery, `/internal/tick`)
- server-side request forgery through the External Request node, outbound
  webhooks or media fetching
- injection of any kind, including template injection through message
  placeholders and stored XSS through platform-supplied content or uploads
- privilege escalation across the role and permission matrix
- disclosure of stored credentials, in responses, logs, error reports or admin
  pages
- weaknesses in the reference deployment that would make a self-hoster following
  [`docs/self-hosting.md`](docs/self-hosting.md) verbatim less safe than that
  document claims

**Out of scope**

- findings that require an already-compromised host, database or `SECRET_KEY`
- a self-hoster's own misconfiguration — an exposed database port, a wildcard
  `ALLOWED_HOSTS`, a reused key — unless our defaults or documentation led there
- vulnerabilities in a third-party platform's API rather than in our use of it
- missing hardening headers on a deployment that has replaced the reference
  proxy, where the application already sets them itself
- automated scanner output with no demonstrated impact, and reports about
  version strings, rate limits on unauthenticated read-only endpoints, or
  self-XSS
- social engineering, physical access, and denial of service by volume

## How this project defends itself

[`docs/SECURITY-BASELINE.md`](docs/SECURITY-BASELINE.md) is the per-PR security
checklist every change is reviewed against: tenancy isolation, untrusted inbound
content, the template-injection ban, public token routes, secrets handling, the
SSRF guard, input limits, web platform hardening, file uploads and supply chain.
CI enforces the automatable parts — an IDOR fuzz suite over every registered
route, `pip-audit` and `npm audit`, a secret scan, and security lint rules — and
a security review runs over each layer's merged diff.

If you are reporting something that the baseline already names, saying which
item helps us confirm whether it is a gap in the rule or a gap in an
implementation of it.

## Deploying safely

The reference deployment is hardened by default and
[`docs/self-hosting.md`](docs/self-hosting.md) carries an operator hardening
checklist. The two things worth repeating here: the application refuses to boot
in production without a real `SECRET_KEY` and `ENCRYPTION_KEY_SALT`, and a
database dump contains your encrypted platform credentials — so back those two
keys up somewhere other than the dump they decrypt.
