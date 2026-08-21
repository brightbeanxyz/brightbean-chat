# ---------------------------------------------------------------------------
# Frontend — compile the Tailwind bundle.
# ---------------------------------------------------------------------------
# The output has to exist before collectstatic runs in the runtime stage:
# production uses CompressedManifestStaticFilesStorage, which hard-fails on a
# {% static %} reference it cannot resolve. It is also gitignored, so it cannot
# simply arrive with the source copy.
#
# The vendored JS in static/js/vendor/ is committed and needs no build step —
# see scripts/vendor-js.mjs for why.
FROM node:20-slim AS frontend

WORKDIR /app

# Lockfile first, so a source-only change reuses the install layer.
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

# styles.css scans templates/ and apps/**/templates/ through its @source
# directives, so Tailwind needs the whole tree to know which classes to emit.
COPY . .
RUN npm run build:css \
    && test -s theme/static/css/dist/styles.css

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
