<p align="center">
  <a href="https://github.com/brightbeanxyz/brightbean-chat">
    <img src="static/img/brightbean-chat-logo.webp" alt="BrightBean Chat" width="280">
  </a>
</p>

<p align="center">
  <strong>The open-source ManyChat alternative. Chat-marketing automation you host yourself.</strong>
</p>

<p align="center">
  Build flows once and run them across Telegram, Instagram, Messenger, WhatsApp,
  SMS, and email, with a visual builder, shared inbox, broadcasts, sequences,
  analytics, and a public API.
</p>

<p align="center">
  <a href="https://github.com/brightbeanxyz/brightbean-chat/actions/workflows/ci.yml"><img src="https://github.com/brightbeanxyz/brightbean-chat/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-AGPL--3.0-blue.svg" alt="License: AGPL-3.0"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.12%2B-blue.svg" alt="Python 3.12+"></a>
  <a href="https://www.djangoproject.com/"><img src="https://img.shields.io/badge/Django-5.x-green.svg" alt="Django 5.x"></a>
</p>

---

## About BrightBean Chat

BrightBean Chat aims at ManyChat's feature set: keyword triggers, comment-to-DM,
story mentions, drip sequences, broadcasts, a shared inbox, and a visual flow
builder. The difference is where it runs. You bring your own server and your own
platform developer apps, and in exchange there is no per-contact pricing, no
plan that gates the feature you need, and nobody sitting between you and your
audience.

The application calls each platform's official API with your credentials.
There is no aggregator in the middle and no payment provider to sign up for.
Every installation has every feature.

> **Status: pre-1.0.** The core platform is in place: tenancy and RBAC, all six
> channel adapters, the flow engine and builder, contacts, the inbox,
> sequences, broadcasts, analytics, the media library, and the public API.
> Remaining work is tracked in [`docs/ROADMAP.md`](docs/ROADMAP.md). Read
> [`SECURITY.md`](SECURITY.md) before pointing a real audience at it.

## How it compares to ManyChat

| | ManyChat | BrightBean Chat |
|---|---|---|
| **Hosting** | Managed SaaS | Self-hosted: Docker Compose on your own server, or one-click on Railway |
| **Pricing** | Per-contact tiers, with features gated by plan | One tier, every feature, no contact limits. Your only cost is the server |
| **Your data** | Lives in ManyChat's account | Your PostgreSQL and your object storage |
| **Platform access** | Through ManyChat's Meta app | Your own Meta, Twilio, and SMTP credentials, called directly. No aggregator |
| **Source** | Proprietary | AGPL-3.0, so you can read it, change it, and run your fork |
| **Extensibility** | Catalog of native integrations | Public REST API, signed outbound webhooks, and the External Request node |
| **Channels** | Instagram, Messenger, WhatsApp, Telegram, SMS, email, TikTok | The same, minus TikTok, whose DM API is restricted to badged partners |
| **AI features** | Built-in reply generation and intent detection | None, deliberately ([`docs/SPEC.md`](docs/SPEC.md) §1.1) |

Self-hosting has costs ManyChat absorbs for you. ManyChat keeps the service
running, and its Meta app is already approved. Running this yourself means
creating your own Meta developer app, passing app review for the permissions
each channel needs, and owning uptime, upgrades, and backups. If that is not a
trade you want to make, ManyChat is the better product for you.

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

Meta's platforms need a developer app of your own, set as environment
variables. See [Platform credentials](#platform-credentials). Per-channel
webhook URLs, permissions, and platform quirks are in
[`docs/channels/`](docs/channels/).

## Try it locally

To see the flow builder, inbox, contacts, and campaign surfaces, run the
development stack:

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

A running instance serves the design system's style guide at
<http://localhost:8000/ui/>.

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

### One click on Railway

[![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/deploy/brightbean-chat?referralCode=niwfCQ&utm_medium=integration&utm_source=template&utm_campaign=generic)

The template provisions PostgreSQL, the web service, and the worker, generates
the two crypto secrets once and shares them, and points the app at its own
generated domain. You supply one thing: an S3-compatible bucket.

That bucket is not optional. A Railway volume attaches to a single service, so
the web and worker processes share no filesystem. Without `STORAGE_BACKEND=s3`
a queued contact import cannot find the file the web process wrote, and
uploaded media disappears on the next restart. AWS S3, Cloudflare R2, Backblaze
B2, and MinIO all work through the same five `S3_*` variables. The
[Railway guide](docs/self-hosting.md#railway) has a worked R2 example and the
manual three-service setup if you would rather build it yourself.

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

## Run it from source

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

`make setup` copies `.env.example`, installs the development dependencies,
builds the frontend bundles, and runs migrations. Then use two
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

## Platform credentials

Instagram, Facebook Messenger and WhatsApp each need a Meta developer app of
your own. There is no shared app to borrow and nothing to register with us.

### Two fields, both called "callback"

Meta asks for two different URLs and calls them similar things. They are not
interchangeable, and putting the OAuth one in the webhook field fails with
*"The callback URL or verify token couldn't be validated."*

| Meta's field | Value |
|---|---|
| **Callback URL**, under the use case's *Configure webhooks* step, next to a **Verify token** box | `https://<your-host>/webhooks/<platform>/` |
| **Redirect URI**, under *Set up Instagram business login* (Instagram) or **Valid OAuth Redirect URIs** (Messenger) | `https://<your-host>/channels/<platform>/callback/` |

`<platform>` is `instagram`, `messenger` or `whatsapp`. WhatsApp has only the
first: it never runs OAuth.

One URL each per deployment, not one per workspace. Meta matches them character
for character, and the workspace travels in a signed `state` instead.

Instagram and Messenger keep **separate** allowlists, because they are separate
OAuth systems: Instagram authorizes at `www.instagram.com`, Messenger at
`www.facebook.com`. The **Redirect URI Validator** on the *Facebook Login for
Business* page therefore only checks Messenger's list. Paste Instagram's
callback into it and it answers *"This is an invalid redirect URI for this
application"* however Instagram is configured, because Instagram's URI is not
supposed to be on that list. It goes under *Instagram → Set up Instagram
business login* instead.

### Use cases and permissions

The App Dashboard is organized by use case. Add the use case, then open its
*Permissions and features* page and add the permissions.

| Channel | Use case, as the picker names it | Permissions to add |
|---|---|---|
| Instagram | Manage messaging & content on Instagram | `instagram_business_basic`, `instagram_business_manage_messages`, `instagram_business_manage_comments` |
| Messenger | Engage with customers on Messenger from Meta | `pages_messaging` |
| Messenger | Manage everything on your Page | `pages_manage_metadata`, `pages_read_engagement`, `pages_manage_engagement` |
| WhatsApp | Connect with customers through WhatsApp | none here, the two WhatsApp permissions go on a system user token |

Messenger needs both of its use cases: `pages_messaging` is not offered under
the Page use case, and the Page use case is the only place the rest appear.
`pages_show_list` comes with it automatically and needs no action.

Once a use case is added, the dashboard's use case switcher shortens its name.
"Engage with customers on Messenger from Meta" becomes "Messenger from Meta".
Same use case.

Nothing else in the picker applies. Marketing API, ads MCP server, Audience
Network, app ads, Catalog, Threads, Instant Games, Live Video, oEmbed,
fundraiser, data portability and ThreatExchange are all unrelated, and each one
you add is another permission set to justify at App Review.

### Instagram

Runs on the Instagram API with Instagram Login, against professional accounts,
Business or Creator. No Facebook Page in the middle.

1. Create an app and add the use case **Manage messaging & content on
   Instagram**.
2. *Permissions and features*: add the three `instagram_business_*` permissions
   above. The dashboard's **Add all required permissions** button adds exactly
   those three.
3. *Instagram → Set up Instagram business login*: open **Business login
   settings** and put `https://<your-host>/channels/instagram/callback/` in the
   **Redirect URI** field.

   That screen leads with an embed URL to drop into an anchor tag or button on
   your own website. Ignore it. This deployment builds its own authorize URL
   and starts the flow from *Settings → Channels → Instagram*, so the redirect
   URI is the only field on that screen you need.
4. *Instagram → API setup*: copy the **Instagram app ID** and **Instagram app
   secret**, which are not the Facebook app id and secret, then set:

   ```dotenv
   PLATFORM_INSTAGRAM_CLIENT_ID=...
   PLATFORM_INSTAGRAM_CLIENT_SECRET=...
   PLATFORM_INSTAGRAM_VERIFY_TOKEN=...   # any long random string you choose
   ```
5. *Instagram → Configure webhooks*: callback URL
   `https://<your-host>/webhooks/instagram/`, verify token from step 4, then
   **Verify and save** and subscribe to `messages`, `messaging_postbacks`,
   `comments`, `mentions` and `message_deletions`.

All three permissions are requested at connect time rather than incrementally,
because connecting without `manage_comments` succeeds and then fails every
public reply. Verification passes while the app is in development, but no
deliveries arrive until it is published. See
[`docs/channels/instagram.md`](docs/channels/instagram.md).

### Facebook Messenger

1. Create an app of type **Business**, add both Messenger use cases, and add the
   **Facebook Login for Business** product.
2. *Permissions and features*, on each use case: add `pages_messaging` to the
   Messenger one, and the three `pages_*` permissions to the Page one.
3. *Facebook Login for Business → Settings*: add
   `https://<your-host>/channels/messenger/callback/` to **Valid OAuth Redirect
   URIs**. Strict Mode is forced on, so the match is exact. The **Redirect URI
   Validator** at the top of that page checks a URI before you save it.
4. *App settings → Basic*: copy the app ID and app secret, then set:

   ```dotenv
   PLATFORM_MESSENGER_CLIENT_ID=...
   PLATFORM_MESSENGER_CLIENT_SECRET=...
   PLATFORM_MESSENGER_VERIFY_TOKEN=...
   ```
5. *Messenger API Settings → Configure webhooks*: callback URL
   `https://<your-host>/webhooks/messenger/`, verify token from step 4, then
   **Verify and save**.

Skip the dashboard's **Generate access tokens** step. This deployment connects
pages through its own OAuth flow and subscribes each one to `messages`,
`messaging_postbacks`, `messaging_referrals`, `message_deliveries`,
`message_reads` and `feed` at connect time. Without `pages_manage_metadata`
that subscription fails, and a page then connects and silently never delivers.
See [`docs/channels/messenger.md`](docs/channels/messenger.md).

### WhatsApp

No OAuth. You paste a system user token into the connect page rather than
authorizing through Meta. The app secret is still required, because it is what
every inbound delivery's signature is verified against.

1. Create an app of type **Business** and add the use case **Connect with
   customers through WhatsApp**. It needs a business portfolio.
2. *WhatsApp → API Setup*: add a phone number and verify it. Note the **phone
   number ID**, which is the numeric API id rather than the number itself, and
   the **WhatsApp Business Account ID**.
3. *Business Settings*: create a system user, give it access to the WABA, and
   generate a token carrying `whatsapp_business_messaging` and
   `whatsapp_business_management`. Choose **never expires**.
4. *App settings → Basic*: copy the app ID and app secret, then set:

   ```dotenv
   PLATFORM_WHATSAPP_CLIENT_ID=...
   PLATFORM_WHATSAPP_CLIENT_SECRET=...
   PLATFORM_WHATSAPP_VERIFY_TOKEN=...
   ```
5. *WhatsApp → Configuration*: callback URL
   `https://<your-host>/webhooks/whatsapp/` and the verify token from step 4.

Those two permissions ride on the token rather than on the use case's permission
list, because WhatsApp runs no login dialog and nobody is ever shown a consent
screen. A 60-day token becomes a silent outage two months after launch, which is
why step 3 says never expires. Finish at *Settings → Channels → WhatsApp → set
it up*, which reads the number back from Meta to prove the token before storing
anything. See [`docs/channels/whatsapp.md`](docs/channels/whatsapp.md).

### Environment variables

| Variable | Required | Description |
|---|---|---|
| `PLATFORM_INSTAGRAM_CLIENT_ID` | For Instagram | Instagram app ID, from *Instagram → API setup* |
| `PLATFORM_INSTAGRAM_CLIENT_SECRET` | For Instagram | Instagram app secret. Neither value works without the other |
| `PLATFORM_INSTAGRAM_VERIFY_TOKEN` | For Instagram | Any long random string you choose, pasted into Meta's *Verify token* box |
| `PLATFORM_MESSENGER_CLIENT_ID` | For Messenger | Facebook app ID, from *App settings → Basic* |
| `PLATFORM_MESSENGER_CLIENT_SECRET` | For Messenger | Facebook app secret |
| `PLATFORM_MESSENGER_VERIFY_TOKEN` | For Messenger | As above, for the Messenger webhook |
| `PLATFORM_WHATSAPP_CLIENT_ID` | For WhatsApp | Meta app ID that owns the WhatsApp Business account |
| `PLATFORM_WHATSAPP_CLIENT_SECRET` | For WhatsApp | Meta app secret. Required even though WhatsApp runs no OAuth, because it verifies every inbound signature |
| `PLATFORM_WHATSAPP_VERIFY_TOKEN` | For WhatsApp | As above, for the WhatsApp webhook |

Meta's console labels the first two of each *App ID* and *App Secret*, and
`PLATFORM_<PLATFORM>_APP_ID` and `_APP_SECRET` are accepted as aliases. The full
variable reference is [`.env.example`](.env.example).

### How credentials resolve

These are developer credentials. They belong in the deployment's `.env` beside
`SECRET_KEY` and `DATABASE_URL`, not in a settings page: no workspace admin
should be able to change which Meta app the deployment speaks as.

A deployment serving several organizations can give each its own app id and
secret from the Django admin, at `/admin/` → *Credentials → Platform
credentials*, which is superuser-only. The environment wins. When a platform is
configured in both places the admin row is never consulted, so it is a fallback
for organizations that have none rather than an override. A level is used only
when it is complete, so a secret without its id leaves that platform
unconfigured rather than half-configured.

`PLATFORM_<PLATFORM>_VERIFY_TOKEN` is the exception and is always
deployment-level, including when the app id and secret come from an organization
row. Meta's verification `GET` arrives unauthenticated and names no
organization, so the endpoint reads only the environment. Leave it unset and
that platform's verification answers 404, which means the webhook cannot be
subscribed and no inbound events arrive at all.

Telegram, SMS and email have no deployment-level credentials. A BotFather token,
a Twilio account SID and an SMTP login all live on the individual channel
connection.

### What Meta has to approve

A new app starts with **Standard Access**, which reaches only accounts whose
users hold a role on the app: the owner, and anyone added as a developer or
tester. That is enough to build against and enough for a self-hoster running
their own accounts, so until App Review, connect an account that holds one of
those roles. For Instagram, assign the account the **Instagram Tester** role
under the app's *Roles* tab first.

Connecting an account belonging to someone else means asking Meta for **Advanced
Access** on each permission through **App Review**, backed by **Business
Verification** of the business behind the app.

| Platform | Permissions to request | Also required |
|---|---|---|
| **Instagram** | All three `instagram_business_*` permissions | App Review, submitted per permission with a screencast showing each one used end to end; Business Verification |
| **Facebook Messenger** | `pages_messaging`, plus `pages_read_engagement` and `pages_manage_engagement` if you use the comment trigger | Business Verification, a privacy policy URL, and a **Data Deletion Request URL** under *Facebook Login for Business → Settings* |
| **WhatsApp** | `whatsapp_business_messaging`, `whatsapp_business_management`, on the system user token | No App Review, since a system user token has no consent screen to review. Business Verification and message quality set the per-number messaging limit, and every template is approved individually |

Budget weeks rather than days, and expect at least one rejection asking for a
clearer screencast. This is Meta's process; nothing in this product changes it.

Telegram, SMS and email have nothing to request. A BotFather token, a Twilio
account SID and auth token, and an SMTP or provider key are all issued on the
spot and reach every contact from the first message.

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
| Deployment | Docker Compose, Caddy, and a one-click Railway template |

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
security baseline are part of the development workflow, not background reading.
New endpoints must be tenant-scoped, new URLs must follow the project routing
conventions, and security-sensitive behavior needs a regression test.

## Security

Please do not report vulnerabilities through public issues. Follow the process
in [`SECURITY.md`](SECURITY.md), which explains the supported versions, scope,
safe-harbor terms, and what to include in a report.

## License

[GNU Affero General Public License v3.0](LICENSE). If you run a modified copy as
a network service, the AGPL requires you to offer its corresponding source to
the people who use that service.
