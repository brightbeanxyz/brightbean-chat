# BrightBean Chat

Open-source, self-hostable chat-marketing automation — Django 5, HTMX, Alpine,
Tailwind 4 and PostgreSQL, with no Redis and no message broker.

> **Status: early.** The skeleton (issue #2), multi-tenancy, RBAC,
> authentication and the platform-credential store (issue #31), and the UI shell
> and design system (issue #32) have landed — you can sign up, invite a team and
> switch workspaces, in the real interface. Every domain feature follows in the
> issues tracked from [`docs/ROADMAP.md`](docs/ROADMAP.md).

## Documentation

| Document | What it covers |
|---|---|
| [`docs/SPEC.md`](docs/SPEC.md) | The engineering specification — authoritative |
| [`docs/ROADMAP.md`](docs/ROADMAP.md) | Build layers, workstreams and interface contracts |
| [`docs/SECURITY-BASELINE.md`](docs/SECURITY-BASELINE.md) | The per-PR security checklist |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | Tenant scoping, the IDOR suite, URL and RBAC conventions |

## Quickstart (Docker)

```bash
docker compose up
```

That builds the image, waits for Postgres, runs migrations and serves the app
on <http://localhost:8000>. No `.env` is required — every setting has a
development default. `/healthz` reports the database round-trip.

Sign up at `/accounts/signup/`; the first account gets its own organization and
workspace and lands on `/w/<workspace-id>/`. In development, email goes to the
console, so the verification message appears in `docker compose logs`.

## Quickstart (local Python)

Requires Python 3.12, Node 20+ and a PostgreSQL 16 server.

```bash
python -m venv .venv && source .venv/bin/activate
make setup
make server
```

`make help` lists every target. The design system is at
<http://localhost:8000/ui/>.

### Frontend

Tailwind 4 through plain npm scripts — there is no `django-tailwind` and no
`tailwind.config.js` (Tailwind 4 is CSS-first: the configuration is the
`@source` directives at the top of the stylesheet).

| Path | What it is |
|---|---|
| `theme/static_src/src/styles.css` | The design system. Three token layers in one `:root`; rebranding means editing the ~20 Layer-1 values at the top |
| `theme/static/css/dist/styles.css` | The compiled bundle — a build artefact, gitignored |
| `static/js/vendor/` | htmx, Alpine, flatpickr, Chart.js and Sortable, copied from `node_modules` and committed |

```bash
make frontend     # npm ci + build the bundle and refresh the vendored copies
make css-watch    # rebuild on save, alongside `make server`
```

`make setup` runs `make frontend` for you. The vendored JavaScript is committed,
so a clone with no Node still serves working pages — only the stylesheet needs
building.

Under `docker compose up` the bundle is never missing: the image compiles it in
a Node stage, and the app mounts it as a named volume that Docker seeds from
the image, so the first request is styled even before the `tailwind` watcher has
finished `npm ci`. The watcher then rebuilds into that same volume as you edit,
which also keeps its output off the host — it runs as root, and a bind-mounted
write would leave root-owned files in your checkout. `docker compose down -v`
resets the volume if a stale bundle ever outlives an image rebuild.

To bump a vendored library, change the pin in `package.json`, run
`npm install && npm run vendor`, and commit both the lockfile and the refreshed
files in `static/js/vendor/` — CI re-runs the copy and fails on any difference.

### Dependencies

`requirements.in` and `requirements-dev.in` list direct dependencies and are
the files you edit. `requirements.txt` and `requirements-dev.txt` are compiled
from them and are what actually gets installed: they pin the whole transitive
tree with hashes, so `pip install --require-hashes` verifies every artefact
rather than just naming a version. After changing either input:

```bash
make lock
```

Commit the recompiled files with the change — CI fails if they are stale.

## Development

```bash
make test        # pytest
make lint        # ruff check + ruff format --check (includes the security rules)
make typecheck   # mypy
make audit       # pip-audit + npm audit, and a self-test of the audit gate
```

Every inline `<script>` and `<style>` carries `nonce="{{ request.csp_nonce }}"`,
and there are no inline event handlers — the stylesheet's hover utility classes
exist so there need not be. Nothing loads from a CDN; the CSP is `'self'`
throughout. Tests enforce all three.

CI runs all of the above plus a Docker build and a gitleaks scan. Install the
pre-commit hooks to catch most of it before pushing:

```bash
pip install pre-commit && pre-commit install
```

## Background work

Delays, retries, sequence steps, broadcast fanout and hourly housekeeping are
rows in one Postgres table, claimed by a worker ([`docs/SPEC.md`](docs/SPEC.md)
§15 — there is no Redis, and there never will be). Without one of the two options below, `runserver`
alone serves pages and schedules nothing.

```bash
make worker
```

Run as many as you like: the claim statement uses `FOR UPDATE SKIP LOCKED`, so
concurrent workers take disjoint batches. `Procfile` and `docker-compose.yml`
both carry a worker process already.

On a host with no always-on process, set `TICK_TOKEN` and point a cron service
or uptime pinger at `https://your-host/internal/tick?token=…` once a minute
instead. The route 404s while `TICK_TOKEN` is unset, so leaving it empty exposes
nothing.

## Configuration

Copy [`.env.example`](.env.example) to `.env` and edit. Two variables are
mandatory outside development — the settings module refuses to boot without
them:

| Variable | Purpose |
|---|---|
| `SECRET_KEY` | Django's signing key; also the input to field encryption |
| `ENCRYPTION_KEY_SALT` | HKDF salt for the AES-256-GCM encrypted fields |

Optional but worth knowing about: `GOOGLE_AUTH_CLIENT_ID` / `_SECRET` enable the
Google sign-in button, `TRUSTED_PROXIES` tells the auth rate limiter which peers
may set `X-Forwarded-For`, `TICK_TOKEN` enables the cron-driven queue drain
described above, and `PLATFORM_<PLATFORM>_<KEY>` supplies the deployment-level
fallback in the credential chain (workspace override → organization →
environment).

Generate each with `python -c "import secrets; print(secrets.token_urlsafe(50))"`.
Losing `SECRET_KEY` or `ENCRYPTION_KEY_SALT` makes every stored credential
undecryptable, so back them up with the database.

## License

[GNU Affero General Public License v3.0](LICENSE).
