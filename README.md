# BrightBean Chat

Open-source, self-hostable chat-marketing automation — Django 5, HTMX, Alpine,
Tailwind 4 and PostgreSQL, with no Redis and no message broker.

> **Status: early.** The skeleton (issue #2) and multi-tenancy, RBAC,
> authentication and the platform-credential store (issue #31) have landed. The
> UI shell and every domain feature follow in the issues tracked from
> [`docs/ROADMAP.md`](docs/ROADMAP.md), so the pages are functional and
> unstyled for now.

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

Requires Python 3.12 and a PostgreSQL 16 server.

```bash
python -m venv .venv && source .venv/bin/activate
make setup
make server
```

`make help` lists every target.

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

CI runs all of the above plus a Docker build and a gitleaks scan. Install the
pre-commit hooks to catch most of it before pushing:

```bash
pip install pre-commit && pre-commit install
```

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
may set `X-Forwarded-For`, and `PLATFORM_<PLATFORM>_<KEY>` supplies the
deployment-level fallback in the credential chain (workspace override →
organization → environment).

Generate each with `python -c "import secrets; print(secrets.token_urlsafe(50))"`.
Losing `SECRET_KEY` or `ENCRYPTION_KEY_SALT` makes every stored credential
undecryptable, so back them up with the database.

## License

[GNU Affero General Public License v3.0](LICENSE).
