# Self-hosting BrightBean Chat

Everything needed to run this yourself: first boot, TLS, the background worker,
backups, upgrades, and how to tell whether what you deployed is healthy.

The reference deployment is Docker Compose on one machine. There are also
one-click configurations for Heroku, Render and Railway. All four are hardened
out of the box — no default secrets, `DEBUG` off, Postgres closed to the
internet — and this guide will not ask you to relax any of that.

> The security expectations behind every choice here are in
> [`SECURITY-BASELINE.md`](SECURITY-BASELINE.md). This guide points at it rather
> than repeating it; the [hardening checklist](#hardening-checklist) at the end
> is the operator's half.

## Contents

- [What you need](#what-you-need)
- [Two processes, not one](#two-processes-not-one)
- [Docker Compose — the reference deployment](#docker-compose--the-reference-deployment)
- [Connecting your first channel](#connecting-your-first-channel)
- [Verifying the deployment](#verifying-the-deployment)
- [TLS termination](#tls-termination)
- [Storage, when web and worker are separate](#storage-when-web-and-worker-are-separate)
- [Heroku](#heroku)
- [Render](#render)
- [Railway](#railway)
- [Running without a worker (tick mode)](#running-without-a-worker-tick-mode)
- [Environment variables](#environment-variables)
- [Backups](#backups)
- [Upgrades](#upgrades)
- [Hardening checklist](#hardening-checklist)
- [Troubleshooting](#troubleshooting)

---

## What you need

- **A domain name**, with an A (and ideally AAAA) record pointing at the host
  *before* you start. The certificate is issued on first request, and that
  cannot happen until DNS resolves.
- **A host with a public IP**, ports 80 and 443 reachable. 1 vCPU and 1 GB RAM
  runs a small instance; 2 vCPU / 2 GB is the size the performance numbers in
  [`SPEC.md`](SPEC.md) §21 assume.
- **Docker Engine 24+ with the Compose plugin.**
- **Nothing else.** Postgres is the only datastore — it is also the task queue,
  the lock manager and the rate limiter ([`SPEC.md`](SPEC.md) §22). There is no
  Redis and no message broker to run, and any guide that tells you to add one is
  describing a different application.

Webhooks are why the public hostname is not optional. Telegram, Meta and Twilio
all deliver events by POSTing to a URL you register with them, and all of them
require HTTPS. A deployment that is not publicly reachable can send messages but
will never receive one.

## Two processes, not one

The app runs as **two** long-lived processes against one database:

| Process | Command | What it does |
|---|---|---|
| web | `gunicorn config.wsgi:application` | Serves pages, the API, and the webhook endpoints. Executes the first step of a flow *inline* when it can do so within 1.5 seconds. |
| worker | `python manage.py process_tasks` | Claims and runs everything time-based from the queue table. |

Both compose files, the `Procfile` and every PaaS configuration here run both.

**If only the web process is up**, the app looks fine and is quietly half
broken. Inbound webhooks are still acknowledged, and a flow whose first step is
a simple reply still answers immediately — that path runs inside the request.
But everything the engine hands to the queue simply never happens:

- Smart Delay steps and follow-up timers
- send retries, and the recovery of actions abandoned by a crashed process
- sequence steps
- broadcast fanout
- hourly housekeeping (token refresh, stale-execution expiry, log pruning)

Nothing errors. The rows sit in the queue with a due time in the past. If you
cannot run a second process, use [tick mode](#running-without-a-worker-tick-mode),
which is a supported configuration with a documented cost — not an accident.

You can run **more than one** worker. The claim statement uses `FOR UPDATE SKIP
LOCKED`, so concurrent workers take disjoint batches
(`docker compose -f docker-compose.prod.yml up -d --scale worker=3`).

---

## Docker Compose — the reference deployment

Five services: Postgres, a one-shot migration, the web app, the worker, and
Caddy terminating TLS in front of them.

### 1. Clone, on the host

```bash
git clone https://github.com/brightbeanxyz/brightbean-chat.git
cd brightbean-chat
```

### 2. Generate the secrets

```bash
make prod-secrets
```

That prints four values. Two of them — `SECRET_KEY` and `ENCRYPTION_KEY_SALT` —
are what decrypt your stored platform credentials. Put them somewhere safe now,
not after the first backup (see [Backups](#backups)).

If `make` is not installed:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(50))"
```

### 3. Write `.env`

```bash
cp deploy/env.prod.example .env
```

Then fill in the five required values. `APP_DOMAIN` is the hostname you pointed
at this host; it configures Caddy's certificate, Django's `ALLOWED_HOSTS` and
the `APP_URL` that public links are built from, so there is only one place to
get it right.

```dotenv
APP_DOMAIN=chat.example.com
ACME_EMAIL=ops@example.com
SECRET_KEY=…
ENCRYPTION_KEY_SALT=…
POSTGRES_PASSWORD=…
```

> If you had already run the development stack in this checkout, that `.env` is
> the file you are replacing. Compose reads `./.env` for both interpolation and
> the container environment, so a leftover development `DATABASE_URL` would
> point the production app at a database that is not there.

Nothing here defaults. `docker compose` refuses to start while any required
value is missing, and says which one:

```
error while interpolating services.postgres.environment.POSTGRES_PASSWORD:
required variable POSTGRES_PASSWORD is missing a value: set POSTGRES_PASSWORD —
generate one with `make prod-secrets`, see deploy/env.prod.example
```

It stops at the first one it finds, so if several are missing you will see this
more than once — each message names its own variable and points back here.

That is deliberate. A deployment that boots with a placeholder key published in
this repository signs every session and encrypts every credential with a value
anyone can read ([`SECURITY-BASELINE.md`](SECURITY-BASELINE.md) §8).

### 4. Start it

```bash
docker compose -f docker-compose.prod.yml up -d
```

The first run builds the image (a few minutes: it compiles the Tailwind bundle
and the flow-builder island), starts Postgres, runs migrations to completion,
then starts the app, the worker and Caddy. Caddy obtains the certificate on the
first request.

```bash
docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml logs -f
```

`make prod-up`, `make prod-down` and `make prod-logs` are shorthands for the
same three commands.

### 5. Create the first account

Open `https://chat.example.com/accounts/signup/`. The first account gets its own
organization and workspace and lands in the app.

Password reset and address verification need SMTP (`EMAIL_HOST` and friends in
`.env`). Nothing is gated on a verified address, so you can come back to this —
but you cannot reset a forgotten password without it.

---

## Connecting your first channel

Every platform needs a URL to deliver events to. They are all under `/webhooks/`
on your own domain:

| Platform | Webhook URL | Shape |
|---|---|---|
| Telegram | `https://<your-host>/webhooks/telegram/` | One per deployment; the bot token identifies the connection |
| Instagram | `https://<your-host>/webhooks/instagram/` | One per deployment, shared by every workspace's accounts |
| Messenger | `https://<your-host>/webhooks/messenger/` | One per deployment, shared by every workspace's pages |
| WhatsApp | `https://<your-host>/webhooks/whatsapp/` | One per deployment |
| SMS (Twilio) | `https://<your-host>/webhooks/sms/<connection id>/` | One per channel connection |
| Email (Resend) | `https://<your-host>/webhooks/email/resend/<connection id>/` | One per channel connection |
| Email (SES) | `https://<your-host>/webhooks/email/ses/<connection id>/` | One per channel connection |

The connection id for the per-connection rows is shown on the channel's settings
page after you create it.

**The Meta platforms also want a verify token.** Instagram, Messenger and
WhatsApp confirm a webhook subscription with a `GET` before they will send you
anything. Set `PLATFORM_<PLATFORM>_VERIFY_TOKEN` in `.env`, restart, and paste
the same value into Meta's "Verify token" field. A platform with no token
configured answers **404** to that GET, so nothing can be subscribed to it by
accident.

Per-platform setup — where each credential comes from, what to enable in each
dashboard, and the quirks of each API — is in [`channels/`](channels/):
[Telegram](channels/telegram.md) · [Instagram](channels/instagram.md) ·
[Messenger](channels/messenger.md) · [WhatsApp](channels/whatsapp.md) ·
[SMS](channels/sms.md) · [Email](channels/email.md).

---

## Verifying the deployment

```bash
make smoke URL=https://chat.example.com
```

or, with the optional extras:

```bash
scripts/smoke.sh https://chat.example.com \
  --tick-token "$TICK_TOKEN" \
  --db-host chat.example.com \
  --project brightbean-chat
```

It checks the things that are easy to get wrong and hard to notice:

- `/healthz` reports a real database round-trip, not just that the process is up
- plain HTTP redirects to HTTPS
- all four security headers are present with the right values, and Django's CSP
  survived the proxy
- `/internal/tick` answers **404** without a token and with a wrong one
- an unconfigured Meta webhook refuses verification with a 404
- `--db-host`: Postgres is *not* answering on 5432 from outside
- `--project`: the app container is not running as root

Add `--insecure` when the certificate comes from Caddy's internal CA — that is
what you get with `APP_DOMAIN=localhost`, which is how the stack is tested
locally and in CI.

`/healthz` is also what you point an uptime monitor at. It returns 503 when the
database is unreachable, and it is exempt from the HTTPS redirect so in-network
probes reaching the container directly are not answered with a 301.

---

## TLS termination

### Caddy, the default

Caddy obtains and renews the certificate from Let's Encrypt automatically and
sets the four headers [`SECURITY-BASELINE.md`](SECURITY-BASELINE.md) §8 requires
at the proxy: HSTS, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`
and a referrer policy. `config/settings/production.py` sets the same four
itself, so a deployment is hardened even if the proxy is replaced.

Certificates live in the `caddy_data` volume. Keep it: losing it means
re-issuing on every restart, which runs into Let's Encrypt's rate limits.

Caddy writes **no access log** by default, and that is on purpose — the default
access-log format records the full URI, and `/internal/tick?token=…` and Meta's
`hub.verify_token` both travel in query strings. The application log is scrubbed
([`SECURITY-BASELINE.md`](SECURITY-BASELINE.md) §5); an edge access log is not.
If you want one anyway, add a `log` block to `deploy/Caddyfile` and treat the
output as sensitive.

### A local certificate, for testing

Set `APP_DOMAIN=localhost` and Caddy issues from its own internal CA with no
ACME call at all. This is how you can exercise the whole production stack —
gunicorn, the worker, TLS, the headers — on a laptop before pointing DNS at
anything:

```bash
docker compose -f docker-compose.prod.yml -p bbchat-prod up -d
scripts/smoke.sh https://localhost --insecure --project bbchat-prod
```

### Terminating TLS somewhere else

If you already run nginx, Traefik, HAProxy or a cloud load balancer:

```bash
docker compose -f docker-compose.prod.yml \
               -f deploy/docker-compose.external-tls.yml up -d
```

Caddy is excluded and the app is published on `127.0.0.1:8000` — loopback only,
never `0.0.0.0`, which would put an app that believes it is behind TLS directly
on the internet over plain HTTP.

Your proxy then owns four things. They are listed in full at the top of
[`deploy/docker-compose.external-tls.yml`](../deploy/docker-compose.external-tls.yml);
the two that break the app immediately if you miss them are
`X-Forwarded-Proto: https` (without it Django redirects to itself forever) and
passing the original `Host` header through unmodified (without it every request
is a 400). Set `TRUSTED_PROXIES` to your proxy's address, and re-run the smoke
script — it checks whatever is actually in front.

---

## Storage, when web and worker are separate

The two processes share more than a database. A CSV contact import is
**uploaded by the web process and opened by the worker**
(`apps/contacts/views.py` writes the file, `apps/contacts/imports.py` reads it),
and every media-library upload is served back later by whichever process gets
the request.

With `STORAGE_BACKEND=local` that only works if both processes see the same
filesystem:

| Target | Do they? | What to do |
|---|---|---|
| Docker Compose | **Yes.** `app` and `worker` both mount the `media_data` volume. | Nothing. `local` is fine. |
| Heroku | **No.** Each dyno has its own ephemeral filesystem, wiped on every restart. | Set `STORAGE_BACKEND=s3` and the `S3_*` variables. |
| Render | **No.** Service filesystems are ephemeral and cannot be shared between services. | Set `STORAGE_BACKEND=s3` and the `S3_*` variables. |
| Railway | **No.** A volume attaches to one service. | Set `STORAGE_BACKEND=s3` and the `S3_*` variables. |

Left on `local`, a PaaS deployment looks healthy and fails in two specific ways:
a queued contact import errors because the worker cannot find the file the web
process just wrote, and uploaded media 404s after the next restart. Neither
shows up until someone tries it.

The `S3_*` names are generic on purpose — AWS S3, Cloudflare R2, Backblaze B2
and MinIO all work with the same five variables:

```dotenv
STORAGE_BACKEND=s3
S3_BUCKET_NAME=brightbean-chat
S3_ACCESS_KEY_ID=…
S3_SECRET_ACCESS_KEY=…
S3_ENDPOINT_URL=            # blank for AWS; set it for R2, B2 or MinIO
S3_REGION_NAME=auto
```

**Every process needs identical values.** On Heroku that is automatic — config
vars belong to the app, not the dyno. On Render they are prompted per service,
because Render does not allow `sync: false` inside an environment group; enter
the same answers on both, or move them into a shared environment group from the
dashboard afterwards. On Railway, use a shared variable rather than typing them
into each service.

Keep the bucket private. Delivery URLs are signed, and
[`SECURITY-BASELINE.md`](SECURITY-BASELINE.md) §9 is why — a public bucket
hands out every uploaded file to anyone who guesses a key.

---

## Heroku

[![Deploy to Heroku](https://www.herokucdn.com/deploy/button.svg)](https://heroku.com/deploy?template=https://github.com/brightbeanxyz/brightbean-chat)

[`app.json`](../app.json) provisions Postgres, generates `SECRET_KEY`,
`ENCRYPTION_KEY_SALT` and `TICK_TOKEN`, and creates both a `web` and a `worker`
dyno. `Procfile`'s release phase runs the migrations on every deploy.

You are prompted for two values, because neither can be guessed:

- `ALLOWED_HOSTS` — the hostname the app answers on. If you name the app
  `my-chat`, that is `my-chat.herokuapp.com`.
- `APP_URL` — the same thing with `https://` in front.

Update both when you attach a custom domain, or Django will answer 400 to
every request on it.

`TRUSTED_PROXIES` is pre-filled with the private ranges Heroku's router lives
in. Leave it: without it every request is attributed to the router rather than
to the caller, and auth rate limiting, API throttling and the webhook signature
ban all collapse into one shared bucket — one caller could keep login and
password reset throttled for everybody.

**Set `STORAGE_BACKEND=s3` and the `S3_*` variables**, or contact imports will
fail and uploaded media will vanish on the next restart — see
[Storage, when web and worker are separate](#storage-when-web-and-worker-are-separate).
The prompt defaults to `local` so the button completes without a bucket; it is
not a working production setting on Heroku.

**Use Basic dynos or larger, both of them.** Eco dynos sleep after 30 minutes of
inactivity. A sleeping web dyno drops the webhook that would have woken it, and
a sleeping worker is no worker at all.

```bash
heroku ps:scale web=1:basic worker=1:basic -a my-chat
heroku logs --tail -a my-chat
```

## Render

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/brightbeanxyz/brightbean-chat)

[`render.yaml`](../render.yaml) creates a Postgres instance closed to the public
internet (`ipAllowList: []`), a Docker web service with `/healthz` as its health
check and `migrate` as its pre-deploy command, and a Docker background worker
running `process_tasks`.

The two crypto secrets live in a shared **environment group**, and both services
read them from it. This matters more than it looks: `generateValue: true`
written directly on each service would generate a *different* value per service,
and every credential the worker encrypted would be undecryptable by the web
process. The deployment would go green and fail on your first channel
connection.

You are prompted for `ALLOWED_HOSTS` and `APP_URL` at deploy time — use
`<your-service>.onrender.com` until you attach a domain.

`TRUSTED_PROXIES` is set for you to the private ranges Render's router lives in,
for the same reason it is on Heroku: without it every request is attributed to
the router and the rate limiters stop telling callers apart.

**Set `STORAGE_BACKEND=s3` and the `S3_*` variables on both services**, with the
same answers on each — Render prompts per service because `sync: false` is not
allowed inside an environment group, so this is the one thing the blueprint
cannot keep in step for you. Move them into a shared environment group from the
dashboard once you have deployed. See
[Storage, when web and worker are separate](#storage-when-web-and-worker-are-separate).

## Railway

Railway's config-as-code applies to one service at a time, so this is a
four-step setup rather than a button. (A one-click template has to be published
from a Railway account; the repository cannot ship one. If you maintain a fork,
publish a template from your project and link it here.)

1. **New Project → Deploy PostgreSQL.**
2. **Add a service from this repository.** It picks up
   [`railway.json`](../railway.json): Dockerfile build, `/healthz` as the health
   check, and `python manage.py migrate --noinput` as the pre-deploy command.
3. **Add a second service from the same repository** for the worker. In its
   settings, set the config-as-code path to `/deploy/railway.worker.json`, which
   replaces the start command with `python manage.py process_tasks` and drops
   the health check (the worker serves no port).
4. **Set the variables on both services.** `DATABASE_URL` is
   `${{Postgres.DATABASE_URL}}`. `SECRET_KEY` and `ENCRYPTION_KEY_SALT` must be
   the **same value on both services** — use a shared variable rather than
   generating each one twice, for the same reason Render uses an env group.
   Also set `DJANGO_SETTINGS_MODULE=config.settings.production`,
   `DJANGO_ENV_FILE=/nonexistent`, `ALLOWED_HOSTS` and `APP_URL`.

   Two more that Railway's config files cannot carry, and that a deployment is
   quietly broken without:

   - `TRUSTED_PROXIES=127.0.0.1/32,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16`, so
     requests are attributed to the caller rather than to Railway's router. Without
     it auth rate limiting, API throttling and the webhook signature ban all share
     one bucket across every user.
   - `STORAGE_BACKEND=s3` and the `S3_*` variables, as shared variables so both
     services agree. A Railway volume attaches to one service, so the worker
     cannot read a contact-import file the web service wrote — see
     [Storage, when web and worker are separate](#storage-when-web-and-worker-are-separate).

Generate a domain for the web service, then put that hostname in `ALLOWED_HOSTS`
and its `https://` form in `APP_URL`.

---

## Running without a worker (tick mode)

Some hosts cannot run a second always-on process. `/internal/tick` is an HTTP
wrapper around one worker cycle for exactly that case: point a scheduler at it
and the queue drains on a timer instead of continuously.

1. Set `TICK_TOKEN` to a long random value (`make prod-secrets` prints one).
2. Point a cron service or uptime pinger at
   `https://<your-host>/internal/tick?token=<TICK_TOKEN>` on a schedule.

The route answers 404 while `TICK_TOKEN` is unset, so leaving it empty exposes
nothing. It is safe to run alongside a real worker — the claim statement makes
overlapping drains correct by construction — so you can add it as a safety net
rather than an alternative.

**What it costs.** Everything time-based becomes as late as the gap between
ticks:

| Scheduler | Granularity | A 1-minute Smart Delay fires |
|---|---|---|
| Uptime pinger / cron-job.org | 1 minute | within ~1 minute |
| `cron` on a host you control | 1 minute | within ~1 minute |
| Heroku Scheduler | 10 minutes | within ~10 minutes |
| Render cron job | 1 minute | within ~1 minute |

One request drains up to 10 actions and gives up after 20 seconds — deliberately
inside gunicorn's 30-second worker timeout, so a tick is never killed mid-batch.
A large backlog therefore needs several ticks to clear, which is another way of
saying: tick mode is a fallback, and a real worker is the answer for anything
with volume.

On a host you control, `python manage.py tick` is the same drain as a one-shot
command (55-second budget, sized for a once-a-minute cron).

---

## Environment variables

[`deploy/env.prod.example`](../deploy/env.prod.example) is the production
template — copy it, do not copy `.env.example` (that one is the development
template, and every value in it is a working local default).

The complete reference, including every optional limit and tunable, is
[`.env.example`](../.env.example) at the repository root. What a production
deployment actually decides:

### Required

| Variable | What it is |
|---|---|
| `SECRET_KEY` | Django's signing key, and the input to credential encryption. The app refuses to boot without it outside `DEBUG`. |
| `ENCRYPTION_KEY_SALT` | HKDF salt for the AES-256-GCM encrypted fields. A *different* random value. |
| `ALLOWED_HOSTS` | Hostnames this deployment answers on. Anything else gets a 400. Derived from `APP_DOMAIN` in the compose stack. |
| `APP_URL` | The public origin, with scheme. Unsubscribe, click-tracking and media links are built from it. |
| `DATABASE_URL` | Postgres connection string. |
| `DJANGO_SETTINGS_MODULE` | `config.settings.production`. |

### Deployment-specific

| Variable | What it is |
|---|---|
| `APP_DOMAIN` | Compose only: the one hostname that configures Caddy, `ALLOWED_HOSTS` and `APP_URL`. |
| `ACME_EMAIL` | Compose only: where Let's Encrypt sends certificate notices. |
| `POSTGRES_PASSWORD` / `POSTGRES_USER` / `POSTGRES_DB` | Compose only: the bundled database. Set before the first start. |
| `IMAGE_TAG` | Which image tag the compose stack runs. The [upgrade](#upgrades) knob. |
| `TRUSTED_PROXIES` | Which peers may set `X-Forwarded-For`. Empty ignores the header entirely, which turns per-caller auth rate limiting into per-deployment. |
| `DJANGO_ENV_FILE` | Set to `/nonexistent` so the environment is the only source of configuration. |

### Platform and behaviour ([`SPEC.md`](SPEC.md) §20)

| Variable | What it is |
|---|---|
| `PLATFORM_<PLATFORM>_CLIENT_ID` / `_CLIENT_SECRET` | Deployment-level app credentials, the last step of the resolution chain (workspace override → organization → here). Meta platforms only. |
| `PLATFORM_<PLATFORM>_VERIFY_TOKEN` | The token Meta checks when you subscribe a webhook URL. Unset means that platform's verification GET answers 404. |
| `TICK_TOKEN` | Shared secret for `/internal/tick`. Unset means the route does not exist. |
| `EXTERNAL_REQUEST_ALLOW_PRIVATE` | Lets the External Request node reach private address ranges, for an on-prem deployment calling services on its own network. It relaxes *only* the private-range rule — loopback, cloud metadata, multicast and this deployment's own host stay denied ([`SECURITY-BASELINE.md`](SECURITY-BASELINE.md) §6). |
| `DEFAULT_SEND_RATE_OVERRIDES` | JSON per-platform send rates, when your app's limits differ from the published defaults. An unknown platform or a non-positive value fails a startup check rather than being ignored. |
| `EMAIL_HOST` and friends | SMTP for password reset and address verification. |
| `STORAGE_BACKEND` / `S3_*` | `local` (a shared volume) or `s3` (S3, R2, B2, MinIO). `local` requires the web and worker processes to share a filesystem, which is true of the compose stack and of no PaaS — see [Storage](#storage-when-web-and-worker-are-separate). |
| `SENTRY_DSN` | Optional error reporting; empty disables it. |

---

## Backups

Two things need backing up, and **they must not live in the same place**.

### The database

```bash
docker compose -f docker-compose.prod.yml exec -T postgres \
  sh -c 'pg_dump -U "$POSTGRES_USER" -Fc "$POSTGRES_DB"' > backup-$(date +%F).dump
```

The user and database names are read from the container's own environment
rather than written out here, so this keeps working if you set `POSTGRES_USER`
or `POSTGRES_DB` in `.env` — hardcoding the defaults would give you a backup of
the wrong database, or no backup at all, and you would find out at restore time.

Restore into a fresh stack:

```bash
docker compose -f docker-compose.prod.yml up -d --wait postgres
docker compose -f docker-compose.prod.yml exec -T postgres \
  sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists' < backup-2026-01-31.dump
docker compose -f docker-compose.prod.yml up -d
```

`--wait` is not optional on the first line. Without it `up -d` returns as soon
as the container has *started*, and on a fresh volume Postgres is still
initialising — so `pg_restore` runs against a server that is not accepting
connections yet and fails for a reason that has nothing to do with your dump.

### The keys

`SECRET_KEY` and `ENCRYPTION_KEY_SALT` are not in the dump, and they are what
decrypt what *is* in it.

**Treat a dump as a secret.** It contains your contacts, your message history,
and the encrypted platform credentials for every channel you have connected —
bot tokens, page access tokens, Twilio credentials, SMTP passwords. Encrypt it
at rest and restrict who can read it.

**Store the keys somewhere else.** A password manager or a secrets service, not
next to the dump. Kept apart, a stolen dump is inert and stolen keys are
useless; kept together they are one compromise. Kept nowhere, a restored dump is
a database full of credentials nobody can read — and the recovery for that is
re-connecting every channel by hand.

### Uploaded media

Media lives in the `media_data` volume (or in your S3 bucket, if
`STORAGE_BACKEND=s3`):

```bash
docker run --rm -v brightbean-chat_media_data:/media -v "$PWD":/backup alpine \
  tar czf /backup/media-$(date +%F).tar.gz -C /media .
```

---

## Upgrades

```bash
git pull
docker compose -f docker-compose.prod.yml build
docker compose -f docker-compose.prod.yml run --rm migrate
docker compose -f docker-compose.prod.yml up -d
```

**There is no published image to pull.** This project builds its image from
source and does not push one to a registry, so `IMAGE_TAG` names the image this
file builds locally — `docker compose pull` has nothing to fetch. Building on
the host is the supported upgrade, and it is what the commands above do.

If you run a fork that *does* publish an image, point `IMAGE_REPOSITORY` at it
in `.env` (`ghcr.io/you/brightbean-chat`, say) and `IMAGE_TAG` at the release;
then `pull` works and you can skip the build:

```bash
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml run --rm migrate   # or: make prod-migrate
docker compose -f docker-compose.prod.yml up -d
```

The one-shot `migrate` service runs the same migrations the stack runs at boot,
so running it explicitly first is belt and braces — it means the new image
starts against a schema that is already current instead of migrating while the
old release is still serving.

Take a database backup before an upgrade that includes migrations. Migrations
are not reversible in general, and the version you roll back to may not
understand the schema the newer one wrote.

Then re-run the smoke script.

---

## Hardening checklist

The application-level items are [`SECURITY-BASELINE.md`](SECURITY-BASELINE.md)'s
job and CI enforces the automatable ones. These are yours:

- [ ] **Firewall the host.** Allow 22, 80 and 443. Nothing else needs to be
      reachable — the compose stack publishes nothing but Caddy's two ports.
- [ ] **Postgres is not published.** `docker compose -f docker-compose.prod.yml ps`
      must show no host port against `postgres`. Verify from outside with
      `scripts/smoke.sh --db-host <your-host>`.
- [ ] **`DEBUG` is off.** `config.settings.production` forces it off before it
      loads anything else, so this is true by construction — but confirm the
      settings module is what you think it is if you customised anything.
- [ ] **`ALLOWED_HOSTS` names your hosts and nothing else.** No `*`, no bare
      `.herokuapp.com` or `.onrender.com` — a wildcard on shared PaaS apex is a
      Host-header attack against every link the app generates.
- [ ] **The secrets are real and unique to this deployment.** The app refuses to
      boot on a blank or placeholder value, but it cannot tell you that you
      pasted the same key into staging.
- [ ] **Back up the keys separately from the dump**, and treat the dump itself as
      a credential store ([Backups](#backups)).
- [ ] **`TRUSTED_PROXIES` names the thing in front and nothing else.** Empty
      means the rate limiters cannot tell callers apart behind a proxy; too wide
      means a caller can forge `X-Forwarded-For` and evade them entirely.
- [ ] **Every webhook URL you register is `https://`.** Signatures protect
      integrity, not confidentiality; message bodies travel in the request.
- [ ] **Keep the image current.** `git pull && docker compose … build` picks up
      dependency updates. CI runs `pip-audit` and `npm audit` on every change
      ([`SECURITY-BASELINE.md`](SECURITY-BASELINE.md) §10), so an out-of-date
      deployment is the only place a known-vulnerable dependency can survive.
- [ ] **Restrict who can reach `/admin/`** if you do not need it exposed, and give
      the Django superuser a password manager entry rather than a memorable
      password.
- [ ] **Rotate `TICK_TOKEN`** if you ever put it in a URL somewhere it might be
      logged — it is a credential, and anyone holding it can make your web
      process do queue work.
- [ ] **Watch `/healthz`** with something that will tell you. It fails closed on
      a database problem, which is the failure you want to hear about first.

Found a vulnerability in the software rather than in a deployment? See
[`SECURITY.md`](../SECURITY.md).

---

## Troubleshooting

**Every request returns 400, including `/healthz`.** The `Host` header is not in
`ALLOWED_HOSTS`. On the compose stack that means `APP_DOMAIN` does not match the
name you are visiting; on a PaaS it means the prompt was answered with the wrong
hostname, or you attached a domain and did not add it.

**The container never becomes healthy and Caddy never starts.** Caddy waits for
the app's health check. Read `docker compose -f docker-compose.prod.yml logs app`
— a boot refusal names exactly which variable is missing, and a 400 in the log
is the `ALLOWED_HOSTS` case above.

**`docker compose up` exits immediately with "required variable … is missing a
value".** That is the intended behaviour with an incomplete `.env`. The message
names the variable.

**The certificate is not issued.** DNS must resolve to this host and port 80
must be reachable from the internet before Caddy can complete the ACME
challenge. `docker compose -f docker-compose.prod.yml logs caddy` says which of
the two failed.

**Messages send, but delays and sequences never fire.** The worker is not
running. `docker compose -f docker-compose.prod.yml ps` should show a `worker`
service; on a PaaS, check the worker dyno/service is scaled above zero. See
[Two processes, not one](#two-processes-not-one).

**A Meta webhook subscription fails verification.** The platform's
`PLATFORM_<PLATFORM>_VERIFY_TOKEN` is unset (the endpoint answers 404) or does
not match what you typed into Meta's dashboard (403).

**Stored credentials stopped decrypting after a restore.** `SECRET_KEY` or
`ENCRYPTION_KEY_SALT` differs from the one in use when they were written. They
are not in the dump; restore them from wherever you put them in step 2.
