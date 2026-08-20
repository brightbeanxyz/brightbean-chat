# Layer 0 — Agent Prompt

Layer 0 is the one-time bootstrap: a single issue, no siblings, nothing to run in parallel with. Everything else in the build depends on it.

**Dispatch:** [#2](https://github.com/brightbeanxyz/brightbean-chat/issues/2) alone. Layer 1 ([#31](https://github.com/brightbeanxyz/brightbean-chat/issues/31) and [#32](https://github.com/brightbeanxyz/brightbean-chat/issues/32), fully parallel) opens the moment it merges.

Written against the real [BrightBean Studio](https://github.com/brightbeanxyz/brightbean-studio) source, so it names the exact files to port **and** the places where copying Studio verbatim would carry over a known defect. Attach `brightbeanxyz/brightbean-studio` (read access) to the session.

---

## Prompt — Issue #2, L0-A

````
You are implementing GitHub issue #2 in the repo `brightbeanxyz/brightbean-chat`: `[L0-A] Project scaffold, Django skeleton, settings, CI, Docker dev, encryption util`.

Read first (all on `main`): `docs/SPEC.md` §2 and §20, `docs/ROADMAP.md` (execution model + conventions), `docs/SECURITY-BASELINE.md` (§§5, 8, 10 are yours), and issue #2 itself. The spec is authoritative; where it is silent, copy the BrightBean Studio pattern.

CONTEXT: BrightBean Chat is an open-source, self-hostable ManyChat clone (Django 5 + HTMX + Alpine + Tailwind 4 + Postgres, no Redis). The repo currently contains only LICENSE, README and docs/. You are building the runnable skeleton that all 29 other issues sit on. Two sibling issues (#31 tenancy/auth, #32 theme/shell) start the moment yours merges, so your job is the shared substrate and nothing more.

REFERENCE REPO: `brightbeanxyz/brightbean-studio` is the sibling project whose conventions we mirror. Port these specific files/patterns:
- `config/` layout: `settings/{base,development,production,test}.py` + `urls.py` + `wsgi.py` + `asgi.py`. Studio uses django-environ (`env = environ.Env(...)`, `environ.Env.read_env(BASE_DIR/".env")`), `BASE_DIR` two levels up, `env.db("DATABASE_URL", default=...)`, INSTALLED_APPS as DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS, WhiteNoise + CompressedManifestStaticFilesStorage in prod and plain StaticFilesStorage in dev/test, and a single `STORAGE_BACKEND` env var ("local"|"s3") switching STORAGES["default"] between FileSystemStorage and S3Boto3Storage with generic `S3_*` env names so Cloudflare R2 works. Copy that shape.
- `config/settings/test.py`'s trick of setting `SECRET_KEY`/`ENCRYPTION_KEY_SALT` env defaults BEFORE `from .base import *`.
- `apps/common/encryption.py` — AES-256-GCM `EncryptedTextField` / `EncryptedJSONField` plus `encrypt_value`/`decrypt_value`, key derived by HKDF-SHA256 from SECRET_KEY with `salt=settings.ENCRYPTION_KEY_SALT`, stored as base64(12-byte nonce ‖ ciphertext). Port near-verbatim, changing only the HKDF `info` constant to `b"brightbean-chat-field-encryption"`.
- `apps/common/managers.py`, `validators.py` (hex-color validator etc.).
- `pyproject.toml` (ruff: target py312, line-length 120, select E/F/I/N/W/UP/B/SIM, ignore E501, isort known-first-party; mypy + django-stubs; pytest `DJANGO_SETTINGS_MODULE="config.settings.test"`; coverage source=["apps"]), `Makefile` (self-documenting `##` comments + help target; setup/server/worker/migrate/migrations/test/test-cov/lint/format/typecheck/docker-*), `requirements.txt` (grouped by comment headers, every non-obvious pin justified in a comment), `conftest.py`, `.pre-commit-config.yaml` (incl. gitleaks + detect-private-key), `.github/workflows/ci.yml` (jobs lint / typecheck / test with a postgres:16-alpine service / build / secrets-scan; actions pinned to commit SHAs with `# vX.Y.Z` comments; `persist-credentials: false`; per-job `timeout-minutes`; concurrency cancel-in-progress).
- `Dockerfile`, `docker-compose.yml`, `docker-compose.override.yml` (dev bind-mounts + a node:20-slim `tailwind` service), `Procfile`.

DELIVER (issue #2's checklist is the source of truth; this is the shape of it):
1. Runnable Django 5 / Python 3.12 project reaching a placeholder page via `docker compose up`, with `/healthz` doing a real DB check (Studio's `/health/` returns a bare JSON ok and does NOT touch the DB — improve on it, and keep the production `SECURE_REDIRECT_EXEMPT` entry for it).
2. `apps/common/` with the encryption fields, managers, validators — plus a `BaseModel` abstract model (`id` UUIDv7 pk, `created_at`, `updated_at`) that every later model inherits. Studio copy-pastes `uuid.uuid4` + timestamp fields into ~40 models and has no `apps/common/models.py`; we are deliberately not repeating that. UUIDv7 is not in the 3.12 stdlib: add a small dependency (`uuid6` or `uuid-utils`) or implement RFC 9562 §5.7 in `apps/common/uuid7.py` with tests asserting monotonicity within a millisecond and correct version/variant bits.
3. Security plumbing (SECURITY-BASELINE §§5, 8, 10):
   - CI additionally runs `pip-audit` and `npm audit` (fail on known-vulnerable deps, waivers documented inline) and Bandit or ruff's S-rules. Add Dependabot config.
   - A global log-scrubbing filter that redacts token/secret-shaped values, installed in ALL environments, with a test asserting an encrypted field's plaintext never reaches captured logs. Studio has no scrubbing at all — this is new work, not a port.
   - Secure settings defaults: DEBUG off outside dev, SECURE_*/session/CSRF cookie flags, and production **refuses to boot** without SECRET_KEY and ENCRYPTION_KEY_SALT. Studio validates ENCRYPTION_KEY_SALT lazily (first encrypted-field access) — implement it as a Django system check or an `ImproperlyConfigured` raise in base settings when `not DEBUG`, so a misconfigured deploy fails at startup instead of at first webhook.
   - A shared signing utility wrapping `django.core.signing` (versioned payloads, optional expiry, constant-time verify, a `generic 404 on failure` helper). Every later public token route (`/u/`, `/c/`, `/o/`, flow-preview links, `/internal/tick`) must use this one implementation — write it now even though no route consumes it yet, and document that contract in its docstring.
4. CSP via django-csp with per-request nonces (Studio uses django-csp 3.x module-level `CSP_*` settings + `CSP_INCLUDE_NONCE_IN=["script-src"]`; if you adopt django-csp 4.x use the `CONTENT_SECURITY_POLICY` dict form and say so in the PR). `'unsafe-eval'` in script-src is required by Alpine's standard build; keep `'unsafe-inline'` for styles only.

DELIBERATE DEVIATIONS FROM STUDIO — do not copy these:
- Studio's `.python-version` says 3.13 while CI, Dockerfile, ruff and mypy all say 3.12. Pick **3.12** everywhere and keep it consistent.
- Studio's Dockerfile is single-stage and runs as **root** with no `.dockerignore`. Yours: multi-stage, a non-root `USER`, and a real `.dockerignore`. (The full production compose/Caddy stack belongs to issue #28 — you only need the dev-grade image plus a build that CI can exercise.)
- Do not install `django-tailwind`; issue #32 wires Tailwind 4 through plain npm scripts. Leave `theme` out of INSTALLED_APPS — #32 adds it.
- No `apps.*` business apps, no `providers/` package, no allauth config, no templates beyond a minimal placeholder + the base 403/404/500 pages. Tenancy is #31; theme is #32. Resist scope creep: if you find yourself writing a model with a `workspace` FK, stop.
- Studio has no `handler403`/`403.html`; add simple, unstyled error templates now (#32 restyles them).

CONSTRAINTS:
- Branch `feat/l1a-scaffold` off `main`; one PR; `Closes #2` in the body.
- Tests accompany the work (pytest, Studio's conventions). CI must be green.
- Migrations: none expected beyond Django's built-ins.
- Follow `docs/SECURITY-BASELINE.md` for anything you touch, and do not weaken it "temporarily".

DEFINITION OF DONE: a fresh clone runs `docker compose up` to a placeholder page; `/healthz` returns ok and fails when the DB is down; `pytest`, `ruff check`, `ruff format --check`, `mypy` and the audit steps all pass in CI; encrypted fields round-trip; production settings refuse to boot without their secrets (tested); the signing util and log scrubber are tested. Report in the PR body which Studio files you ported, and every deviation you made with its reason.
````

---

## Layer 0 gate

Before opening Layer 1:

1. `docker compose up` from a clean clone reaches a placeholder page; `/healthz` returns ok and fails when the DB is down.
2. `pytest`, `ruff check`, `ruff format --check`, `mypy`, `pip-audit` and `npm audit` all green in CI.
3. Encrypted fields round-trip; production settings refuse to boot without `SECRET_KEY` / `ENCRYPTION_KEY_SALT`; the signing util and log-scrubbing filter are tested.
4. The container runs as a non-root user.
