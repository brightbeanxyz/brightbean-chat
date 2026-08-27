# ---------------------------------------------------------------------------
# Frontend — compile the Tailwind bundle and the flow-builder island.
# ---------------------------------------------------------------------------
# Both outputs have to exist before collectstatic runs in the runtime stage:
# production uses CompressedManifestStaticFilesStorage, which hard-fails on a
# {% static %} reference it cannot resolve. Both are also gitignored, so they
# cannot simply arrive with the source copy.
#
# The vendored JS in static/js/vendor/ is committed and needs no build step —
# see scripts/vendor-js.mjs for why.
FROM node:25-slim AS frontend

WORKDIR /app

# Lockfile first, so a source-only change reuses the install layer.
#
# .npmrc comes along because it carries engine-strict=true, which is what turns
# a Node version this project does not support into a failed install rather
# than an EBADENGINE warning. It has to arrive here and not with the `COPY . .`
# below, which runs after npm ci and so would leave the build unguarded.
COPY package.json package-lock.json .npmrc ./
RUN npm ci --no-audit --no-fund

# Only what styles.css actually reads: its own source, and the trees its
# @source directives scan. `COPY . .` here meant an edit to a setting, a test or
# any other file invalidated the CSS build and every layer after it, for a
# bundle that could not possibly have changed.
#
# These paths must track the @source directives in
# theme/static_src/src/styles.css. TestTailwindSourceCoverage in
# apps/common/tests/test_shell.py fails if a template appears somewhere this
# stage does not copy, so the two cannot drift apart silently.
#
# frontend/builder/ is one of those globs — the island's class names live in
# TSX — and it is also the input to the build:js below, so it arrives once and
# serves both.
COPY theme/static_src/ ./theme/static_src/
COPY templates/ ./templates/
COPY frontend/ ./frontend/
RUN npm run build:css \
    && test -s theme/static/css/dist/styles.css

# The flow-builder React island (issue #10). A separate layer from the CSS so a
# stylesheet change does not rebuild the bundle and vice versa.
#
# It inlines static/flows/flow-schema.json, which is committed, so this needs no
# Python. Both outputs are asserted because
# apps/flows/templatetags/flow_builder.py deliberately renders a notice rather
# than raising when one is missing — right for a running app, exactly wrong for
# a release build, which should never get that far. `builder.css` is also the
# tripwire for the asset filename drifting, and the single-file check for a
# Rollup chunk appearing: collectstatic does not rewrite ES-module specifiers,
# so a second chunk would lose its cache-busting silently.
COPY static/flows/flow-schema.json ./static/flows/flow-schema.json
RUN npm run build:js \
    && test -s apps/flows/static/flows/builder/builder.js \
    && test -s apps/flows/static/flows/builder/builder.css \
    && test "$(ls apps/flows/static/flows/builder/*.js | wc -l)" -eq 1

# ---------------------------------------------------------------------------
# Builder — resolve dependencies into a virtualenv we can copy wholesale.
# ---------------------------------------------------------------------------
# Studio's image is single-stage and ships pip's build cache, the dev tooling
# and a compiler toolchain into production. Here the runtime stage gets the
# virtualenv and nothing else. Only the runtime lock is installed: pytest,
# ruff and mypy live in requirements-dev.* and never reach the image.
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app
COPY requirements.txt .
# --require-hashes: every artefact, including the transitive tree, has to match
# a hash recorded in the lock. Without it a version pin only names a release;
# it does not verify that what arrived is that release.
#
# psycopg[binary] and cryptography ship manylinux wheels, so no compiler or
# libpq-dev is needed — which is also why the runtime stage stays minimal.
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    PATH="/opt/venv/bin:$PATH"

# Non-root from here on (Layer-0 gate item 4). Studio's container runs as root.
RUN groupadd --system --gid 1001 app \
    && useradd --system --uid 1001 --gid app --home-dir /app --shell /usr/sbin/nologin app

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY --chown=app:app . .

# The compiled stylesheet, which .gitignore keeps out of the build context.
# Must land before the collectstatic below, and be owned by the app user, which
# is what runs it.
COPY --from=frontend --chown=app:app /app/theme/static/css/dist /app/theme/static/css/dist

# The flow-builder island, for the same two reasons: .gitignore keeps it out of
# the build context, and the collectstatic below has to see it.
COPY --from=frontend --chown=app:app /app/apps/flows/static/flows/builder /app/apps/flows/static/flows/builder

# WORKDIR creates /app as root and COPY --chown only covers the files it
# copies, so the app user could not create staticfiles/ at build time or write
# uploads at runtime. Creating both here also gives the compose media volume
# the right ownership when Docker seeds it from the image.
RUN mkdir -p /app/staticfiles /app/media \
    && chown app:app /app /app/staticfiles /app/media

USER app

# Static files are baked in so the container needs no writable volume for
# them. The placeholders below only satisfy the settings module's boot-time
# checks — collectstatic touches neither the database nor any secret.
RUN DJANGO_SETTINGS_MODULE=config.settings.production \
    SECRET_KEY=collectstatic-build-placeholder \
    ENCRYPTION_KEY_SALT=collectstatic-build-placeholder \
    ALLOWED_HOSTS=localhost \
    DJANGO_ENV_FILE=/nonexistent \
    python manage.py collectstatic --noinput --clear

EXPOSE 8000

# `exec` so gunicorn replaces the shell and becomes PID 1. Without it, whether
# SIGTERM reaches gunicorn depends on the shell choosing to optimise the call
# away — and when it does not, `docker stop` and every rolling deploy hard-kill
# after the grace period, dropping in-flight webhook requests instead of
# draining them.
CMD ["sh", "-c", "exec gunicorn config.wsgi:application --bind 0.0.0.0:${PORT:-8000} --workers 2 --threads 2"]
