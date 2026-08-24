<div align="center">

# BrightBean Chat

**Open-source chat-marketing automation you host yourself.**

Build flows once and run them across Telegram, Instagram, Messenger, WhatsApp,
SMS and email — with a visual builder, a shared team inbox, broadcasts,
sequences and a public API.

Django 5 · HTMX · Tailwind 4 · PostgreSQL. No Redis. No message broker. No SaaS
in the middle.

[![CI](https://github.com/brightbeanxyz/brightbean-chat/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/brightbeanxyz/brightbean-chat/actions/workflows/ci.yml)
[![License: AGPL v3](https://img.shields.io/badge/license-AGPL--3.0-blue.svg)](LICENSE)

[Deploy it](#deploy-it) · [Run it locally](#run-it-locally) · [Documentation](#documentation) · [Contributing](CONTRIBUTING.md)

</div>

---

> **Status: pre-1.0.** Every workstream through Layer 7 has landed or is landing:
> tenancy and RBAC, the six channel adapters, the flow engine and builder,
> contacts, the inbox, sequences, broadcasts, the media library and the public
> API. What is still moving is tracked in [`docs/ROADMAP.md`](docs/ROADMAP.md).
> Read [`SECURITY.md`](SECURITY.md) before pointing a real audience at it.

## What it does

| | |
|---|---|
| **Channels** | Telegram, Instagram, Facebook Messenger, WhatsApp, SMS (bring your own Twilio) and email (SMTP / Resend / SES). One normalized event shape, one send pipeline. |
| **Flow builder** | A React Flow canvas for `send_message`, `condition`, `smart_delay`, `randomizer`, `start_flow`, `external_request`, `data_collection`, `action` and more. Versioned graphs, one published version at a time. |
| **Triggers** | Keywords, default replies, story mentions and replies, comment-to-DM, follows, ref URLs and QR codes, inbox rules, and the public API. |
| **Compliance** | Per-platform messaging windows and policy rules enforced before every send, plus STOP/HELP handling for SMS and one-click unsubscribe for email. |
| **Inbox** | A shared thread view with assignment, labels, reminders, scheduled replies, rules, and human takeover that pauses automation. |
| **Contacts** | Custom fields, tags, segments, import and export, and identities linked across channels. |
| **Broadcasts & sequences** | Eligibility filters, token-bucket pacing, live counters, and multi-step drip campaigns. |
| **Public API** | A REST API with per-key rate limiting plus signed outbound webhooks, so Make, Zapier and n8n scenarios work without a plugin. |
| **Multi-tenancy** | Organizations and workspaces with two membership tiers and a real permission matrix. Cross-tenant access answers 404, and a fuzz suite in CI proves it. |
| **Secure by default** | Encrypted credentials at rest, an SSRF guard on every user-supplied URL, CSP with per-request nonces, and a deployment that refuses to boot on a placeholder secret. |

Deliberately **not** in v1: TikTok, website chat widgets, e-commerce catalogues,
AI reply generation and billing ([`docs/SPEC.md`](docs/SPEC.md) §1.1).

## Screens

The flow builder, the inbox and the broadcast composer are the three worth
looking at. Screenshots are not committed yet — run the app locally and see the
real thing in about a minute:

```bash
docker compose up
```

The design system's living style guide is at `/ui/` on any running instance.

## Deploy it

Every option below is hardened out of the box: no default secrets, `DEBUG` off,
`ALLOWED_HOSTS` never a wildcard, and a database that is not reachable from the
internet. [`docs/self-hosting.md`](docs/self-hosting.md) is the full walkthrough
— first boot, TLS, backups, upgrades and a hardening checklist.

### Docker Compose, on your own machine

```bash
cp deploy/env.prod.example .env && make prod-secrets
```

Fill in the five required values, point your domain at the host, then:

```bash
docker compose -f docker-compose.prod.yml up -d
```

Postgres, a one-shot migration, gunicorn, the queue worker, and Caddy with
automatic HTTPS. Check it with `make smoke URL=https://your-host`.

### One click

[![Deploy to Heroku](https://www.herokucdn.com/deploy/button.svg)](https://heroku.com/deploy?template=https://github.com/brightbeanxyz/brightbean-chat)
[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/brightbeanxyz/brightbean-chat)

Heroku provisions Postgres, generates the keys and runs both a web and a worker
dyno ([`app.json`](app.json)). Render does the same from
[`render.yaml`](render.yaml), with the crypto secrets in a shared environment
group so the web service and the worker agree on them. Railway is a documented
four-step setup rather than a button —
[why, and the steps](docs/self-hosting.md#railway).

**Both processes matter.** The web process answers webhooks and can run a first
reply inline; the worker is what runs delays, retries, sequence steps and
broadcast fanout. A deployment with only the web process looks healthy and
silently never fires anything time-based. If you cannot run two processes,
[tick mode](docs/self-hosting.md#running-without-a-worker-tick-mode) is the
supported fallback.

## Run it locally

```bash
docker compose up
```

That builds the image, waits for Postgres, runs migrations and serves the app on
<http://localhost:8000>. No `.env` is required — every development setting has a
working default. Sign up at `/accounts/signup/`; the first account gets its own
organization and workspace. Email goes to the console, so the verification
message appears in `docker compose logs`.

Prefer to run it on your own Python? `make setup && make server` —
[CONTRIBUTING.md](CONTRIBUTING.md#local-development) has the prerequisites and
the frontend build.

## Documentation

| Document | What it covers |
|---|---|
| [`docs/self-hosting.md`](docs/self-hosting.md) | Deploying and operating it: first boot, TLS, backups, upgrades, hardening |
| [`docs/channels/`](docs/channels/) | Per-platform setup — [Telegram](docs/channels/telegram.md), [Instagram](docs/channels/instagram.md), [Messenger](docs/channels/messenger.md), [WhatsApp](docs/channels/whatsapp.md), [SMS](docs/channels/sms.md), [email](docs/channels/email.md), [media](docs/channels/media.md) |
| [`docs/api/v1.md`](docs/api/v1.md) | The public REST API and outbound webhooks |
| [`docs/flow-templates.md`](docs/flow-templates.md) | The flow export/import format, and how to contribute a template |
| [`docs/SPEC.md`](docs/SPEC.md) | The engineering specification — authoritative |
| [`docs/ROADMAP.md`](docs/ROADMAP.md) | Build layers, workstreams and interface contracts |
| [`docs/SECURITY-BASELINE.md`](docs/SECURITY-BASELINE.md) | The per-PR security checklist |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | Local development, the frontend build, tenant scoping, the IDOR suite, URL and RBAC conventions |
| [`SECURITY.md`](SECURITY.md) | Reporting a vulnerability |

## Contributing

Start with [CONTRIBUTING.md](CONTRIBUTING.md). Before opening a PR:

```bash
make lint typecheck test
```

CI runs those plus a dependency audit, a production Docker build with a
smoke-tested HTTPS stack, and a secret scan.

## License

[GNU Affero General Public License v3.0](LICENSE). Running a modified copy as a
network service means offering its source to the people who use it.
