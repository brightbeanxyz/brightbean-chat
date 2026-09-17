<p align="center">
  <a href="https://github.com/brightbeanxyz/brightbean-chat">
    <img src="static/img/brightbean-chat-logo-small.webp" alt="BrightBean Chat" width="160">
  </a>
</p>

<p align="center">
  <strong>Open-source chat-marketing automation for teams that want to own their stack.</strong>
</p>

<p align="center">
  Build flows once and run them across Telegram, Instagram, Messenger, WhatsApp,
  SMS, and email — with a visual builder, shared inbox, broadcasts, sequences,
  analytics, and a public API.
</p>

<p align="center">
  <a href="https://github.com/brightbeanxyz/brightbean-chat/actions/workflows/ci.yml"><img src="https://github.com/brightbeanxyz/brightbean-chat/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-AGPL--3.0-blue.svg" alt="License: AGPL-3.0"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.12%2B-blue.svg" alt="Python 3.12+"></a>
  <a href="https://www.djangoproject.com/"><img src="https://img.shields.io/badge/Django-5.x-green.svg" alt="Django 5.x"></a>
</p>

<p align="center">
  <strong>Django 5 · HTMX · Tailwind 4 · PostgreSQL</strong><br>
  No Redis. No message broker. No aggregator in the middle.
</p>

<p align="center">
  <a href="#deploy-it">Deploy it</a> ·
  <a href="#run-it-locally">Run it locally</a> ·
  <a href="#api--webhooks">API &amp; webhooks</a> ·
  <a href="#documentation">Documentation</a> ·
  <a href="CONTRIBUTING.md">Contributing</a>
</p>

---

## About BrightBean Chat

BrightBean Chat is an open-source, self-hostable chat-marketing automation
platform. Connect the messaging channels your audience already uses, build
automation in a visual flow editor, and manage contacts, conversations, and
campaigns from one workspace.

It is designed for creators, agencies, and teams that want to own their
messaging stack rather than put customer conversations behind another SaaS
vendor. The application calls each platform's official API directly using your
credentials. There is no aggregator middleman, per-seat pricing, or required
payment provider.

Every self-hosted installation has one tier: all features are available, with
no feature gates or contact limits. An optional Stripe integration exists only
for operators running BrightBean Chat as a paid hosted service; leave its
settings empty — the default — and billing stays invisible
([`docs/billing.md`](docs/billing.md)).

> **Status: pre-1.0.** The core platform is in place: tenancy and RBAC, all six
> channel adapters, the flow engine and builder, contacts, the inbox,
> sequences, broadcasts, analytics, the media library, and the public API.
> Remaining work is tracked in [`docs/ROADMAP.md`](docs/ROADMAP.md). Read
> [`SECURITY.md`](SECURITY.md) before pointing a real audience at it.

## Features

| | |
|---|---|
| **Multi-channel automation** | One normalized event pipeline for Telegram, Instagram, Facebook Messenger, WhatsApp, SMS, and email. |
| **Visual flow builder** | Versioned React Flow graphs with conditions, delays, randomizers, nested flows, external requests, data collection, actions, SMS, and email nodes. |
| **Triggers** | Keywords, default replies, story mentions and replies, comment-to-DM, follows, ref URLs and QR codes, inbox rules, and the public API. |
| **Shared inbox** | Threaded conversations with assignment, labels, reminders, scheduled replies, inbox rules, and human takeover that pauses automation. |
| **Contacts** | Custom fields, tags, segments, cross-channel identities, import, and export. |
| **Sequences & broadcasts** | Multi-step drip campaigns, audience filters, compliance checks, token-bucket pacing, live counters, and cancellation. |
| **Analytics** | Per-node sent, delivered, failed, and clicked counters, plus flow and broadcast statistics. |
| **Public API** | Bearer-authenticated REST endpoints and signed outbound webhooks for Make, Zapier, n8n, and custom integrations. |
| **Multi-tenancy** | Organizations, workspaces, invitations, two-tier RBAC, and enforced tenant isolation. Cross-tenant object access answers 404. |
| **Secure by default** | Encrypted credentials at rest, SSRF protection for user-supplied URLs, CSP nonces, webhook signature checks, opt-out enforcement, and production settings that reject placeholder secrets. |

Deliberately out of scope for v1: AI reply generation, TikTok DM automation,
native mobile apps, e-commerce, website growth widgets, and built-in third-party
CRM/Zapier-style connectors. The integration surface is the public API,
outbound webhooks, and the External Request flow node
([`docs/SPEC.md`](docs/SPEC.md) §1.1).

## Supported channels

Every channel uses the same flow and compliance engine. Unsupported message
blocks are downgraded visibly where possible, and platform-specific rules are
checked before every send.

| Channel | Inbound | Outbound | Important behavior |
|---|---|---|---|
| **Telegram** | Messages, button presses, and `/start` referrals | Text, images, audio, video, files, buttons, and quick replies | No messaging window; contacts must have opted in by messaging the bot. |
| **Instagram** | DMs, comments, story mentions, and story replies | Text, media, cards, galleries, buttons, and quick replies | 24-hour messaging window; comment-to-DM supports one private reply per comment within seven days; no broadcasts. |
| **Facebook Messenger** | DMs, postbacks, referrals, and Page comments | Text, media, cards, galleries, buttons, and quick replies | 24-hour window; approved message tags support permitted outside-window sends and broadcasts. |
| **WhatsApp** | Messages and delivery/read status updates | Text, media, buttons, quick replies, and approved templates | 24-hour window; approved templates are required outside it. Template management is built in. |
| **SMS** | Inbound texts and carrier opt-out keywords | SMS and outbound MMS images | Bring your own Twilio account. STOP/HELP/START handling and GSM-7/UCS-2 segment previews are built in. |
| **Email** | Provider bounce notifications only (not conversations) | HTML and plain-text email, inline images, unsubscribe links, and tracked links | Bring your own SMTP, Resend, or SES credentials. Unsubscribe and hard-bounce suppression are enforced. |

Per-channel credentials, webhook URLs, permissions, and platform quirks are in
[`docs/channels/`](docs/channels/).

## Try it locally

The fastest way to see the flow builder, inbox, contacts, and campaign surfaces
is to run the complete development stack:

```bash
git clone https://github.com/brightbeanxyz/brightbean-chat.git
cd brightbean-chat
docker compose up
```

Docker builds the image, starts PostgreSQL, runs migrations, and serves the app
at <http://localhost:8000>. No `.env` is required for development. Sign up at
`/accounts/signup/`; the first account gets its own organization and workspace.
Email is written to the console, so verification messages are visible in
`docker compose logs`.

The design system's living style guide is available at
<http://localhost:8000/ui/> on a running instance.

## Deploy it

The full [self-hosting guide](docs/self-hosting.md) covers first boot, TLS,
backups, upgrades, storage, tick mode, and hardening. The reference production
stack is Docker Compose and is secure by default: no placeholder secrets,
`DEBUG` off, PostgreSQL closed to the internet, and only Caddy exposed publicly.

### Docker Compose on a VPS

```bash
git clone https://github.com/brightbeanxyz/brightbean-chat.git
cd brightbean-chat
cp deploy/env.prod.example .env
make prod-secrets
```

Fill in `APP_DOMAIN`, `ACME_EMAIL`, `SECRET_KEY`, `ENCRYPTION_KEY_SALT`, and
`POSTGRES_PASSWORD`, then start the stack:

```bash
docker compose -f docker-compose.prod.yml up -d
make smoke URL=https://your-host.example
```

The stack runs PostgreSQL, a one-shot migration, the Gunicorn web process, the
background worker, and Caddy with automatic HTTPS. Uploaded media is stored in
the shared Docker volume. See [storage guidance](docs/self-hosting.md#storage-when-web-and-worker-are-separate)
when web and worker run on separate hosts or a PaaS.

### Platform deployment

| Heroku | Render | Railway |
|:------:|:------:|:-------:|
| [![Deploy to Heroku](https://www.herokucdn.com/deploy/button.svg)](https://heroku.com/deploy?template=https://github.com/brightbeanxyz/brightbean-chat) | [![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/brightbeanxyz/brightbean-chat) | [Railway setup](docs/self-hosting.md#railway) |

Heroku and Render provision the web and worker processes from the repository
configuration. Railway is documented as a multi-service setup. Heroku, Render,
and Railway use ephemeral or non-shared filesystems, so configure S3-compatible
storage such as AWS S3, Cloudflare R2, Backblaze B2, or MinIO for persistent
media and contact-import files.

### The worker is required

The web process handles pages, the API, and inbound webhooks. The worker runs
everything time-based from the PostgreSQL queue:

- smart delays, retries, and crash recovery;
- sequence steps and broadcast fanout; and
- hourly housekeeping such as token refresh, stale-execution expiry, and log pruning.

A deployment with only the web process looks healthy but silently leaves queued
work undone. If your host cannot run a second long-lived process, the supported
[tick mode](docs/self-hosting.md#running-without-a-worker-tick-mode) provides a
cron or HTTP-triggered worker cycle with documented tradeoffs.

## Run it locally

### Prerequisites

- Python 3.12+
- Node.js 24+
- PostgreSQL 16

### Setup

```bash
python -m venv .venv
source .venv/bin/activate
make setup
```

`make setup` copies `.env.example`, installs the hashed development
dependencies, builds the frontend bundles, and runs migrations. Then use two
terminals:

```bash
make server
```

```bash
make worker
```

The frontend uses Tailwind 4 and a single React 19 flow-builder island. During
development, run `make css-watch` and `make js-watch` alongside the server when
you need live CSS or builder bundle rebuilds. `make help` lists every available
target.

## API & webhooks

BrightBean Chat exposes a bearer-authenticated REST API at `/api/v1/` and
signed outbound webhooks for event-driven integrations. The deployment serves
an interactive reference at `/api/v1/docs` and OpenAPI JSON at
`/api/v1/openapi.json`.

Issue an API key from **Settings → Organization → API keys**, then send it as a
Bearer token:

```bash
curl https://chat.example.com/api/v1/contacts \
  -H "Authorization: Bearer bb_..."
```

The API supports contacts, tags, custom fields, flow starts, messages, and
read-only lists of flows, tags, and fields. Requests are
workspace-scoped, rate-limited per key, and subject to the same compliance and
permission checks as the web UI. Outbound webhook deliveries use HMAC-SHA256,
retry through the worker, and automatically disable after repeated failures.

See the [API reference](docs/api/v1.md) for authentication, scopes, pagination,
errors, endpoints, webhook verification, and a complete integration scenario.

## Tech stack

| Layer | Technology |
|---|---|
| Backend | Python 3.12+, Django 5.x, Gunicorn |
| UI | Django templates, HTMX, Alpine.js, Tailwind CSS 4 |
| Flow builder | React 19, React DOM, `@xyflow/react`, Vite, TypeScript |
| Data and queue | PostgreSQL 16; the database also provides locking, rate limiting, and scheduled-action storage |
| Deployment | Docker Compose, Caddy, Heroku, Render, and Railway configurations |

## Project structure

```text
brightbean-chat/
├── apps/
│   ├── accounts/          # Authentication and sessions
│   ├── organizations/     # Organizations and workspaces
│   ├── members/           # Invitations and RBAC
│   ├── channels/          # Platform connections, adapters, and webhooks
│   ├── flows/             # Flow models, builder API, engine, and triggers
│   ├── contacts/          # CRM, tags, fields, and segments
│   ├── inbox/             # Shared conversations and human takeover
│   ├── campaigns/         # Sequences
│   ├── broadcasts/        # Broadcast composer and fanout
│   ├── analytics/         # Flow and broadcast counters
│   ├── media_library/     # Workspace-scoped uploads and media
│   └── api/               # REST API and outbound webhooks
├── frontend/builder/      # The application's React island
├── templates/             # Django templates and UI components
├── theme/                 # Tailwind source and design tokens
├── docs/                  # Product, deployment, channel, and API docs
├── docker-compose.yml     # Development stack
├── docker-compose.prod.yml # Hardened production stack
└── Makefile               # Setup, development, checks, and deployment helpers
```

## Documentation

| Document | What it covers |
|---|---|
| [`docs/self-hosting.md`](docs/self-hosting.md) | Production deployment, TLS, storage, backups, upgrades, tick mode, and hardening |
| [`docs/channels/`](docs/channels/) | Setup for [Telegram](docs/channels/telegram.md), [Instagram](docs/channels/instagram.md), [Messenger](docs/channels/messenger.md), [WhatsApp](docs/channels/whatsapp.md), [SMS](docs/channels/sms.md), [email](docs/channels/email.md), and [media](docs/channels/media.md) |
| [`docs/api/v1.md`](docs/api/v1.md) | REST API, authentication, rate limits, and outbound webhooks |
| [`docs/flow-templates.md`](docs/flow-templates.md) | Flow export/import format and shareable templates |
| [`docs/SPEC.md`](docs/SPEC.md) | Authoritative engineering and product specification |
| [`docs/ROADMAP.md`](docs/ROADMAP.md) | Build layers, workstreams, and interface contracts |
| [`docs/SECURITY-BASELINE.md`](docs/SECURITY-BASELINE.md) | Per-PR security checklist |
| [`docs/security-audit.md`](docs/security-audit.md) | Security-baseline traceability and open gaps |
| [`docs/pentest-runbook.md`](docs/pentest-runbook.md) | Probing and verifying a self-hosted instance |
| [`tests/acceptance/README.md`](tests/acceptance/README.md) | SPEC §21 acceptance criteria and manual runbooks |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | Local development, frontend conventions, tenant scoping, and review checks |
| [`SECURITY.md`](SECURITY.md) | How to report a vulnerability |

## Running checks

```bash
make lint       # ruff lint and format check, including security rules
make typecheck  # mypy type check
make test       # pytest in parallel
make audit      # pip-audit, npm audit, and the audit-gate self-test
npm run typecheck:js  # TypeScript check for the flow-builder island
npm run test:js       # Vitest tests for the flow-builder island
```

Before opening a pull request, run at least:

```bash
make lint typecheck test
```

CI also runs the JavaScript tests, frontend build checks, migration consistency
checks, dependency audits, and deployment-focused validation.

## Contributing

Start with [`CONTRIBUTING.md`](CONTRIBUTING.md). The project specification and
security baseline are part of the development workflow, not background reading:
new endpoints must be tenant-scoped, new URLs must follow the project routing
conventions, and security-sensitive behavior needs a regression test.

## Security

Please do not report vulnerabilities through public issues. Follow the process
in [`SECURITY.md`](SECURITY.md), which explains the supported versions, scope,
safe-harbor terms, and what to include in a report.

## License

[GNU Affero General Public License v3.0](LICENSE). If you run a modified copy as
a network service, the AGPL requires you to offer its corresponding source to
the people who use that service.
