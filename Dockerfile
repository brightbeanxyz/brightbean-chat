# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Builder — resolve dependencies into a virtualenv we can copy wholesale.
# ---------------------------------------------------------------------------
# Studio's image is single-stage and ships pip's build cache, the dev tooling
# and a compiler toolchain into production. Here the runtime stage gets the
# virtualenv and nothing else. Only requirements.txt is installed: pytest,
# ruff and mypy live in requirements-dev.txt and never reach the image.
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app
COPY requirements.txt .
# psycopg[binary] and cryptography ship manylinux wheels, so no compiler or
# libpq-dev is needed — which is also why the runtime stage stays minimal.
RUN pip install --no-cache-dir -r requirements.txt

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

USER app

# Static files are baked in so the container needs no writable volume for
# them. The placeholders below only satisfy the settings module's boot-time
# checks — collectstatic touches neither the database nor any secret.
RUN DJANGO_SETTINGS_MODULE=config.settings.production \
    SECRET_KEY=collectstatic-build-placeholder \
    ENCRYPTION_KEY_SALT=collectstatic-build-placeholder \
    DJANGO_ENV_FILE=/nonexistent \
    python manage.py collectstatic --noinput --clear

EXPOSE 8000

CMD ["sh", "-c", "gunicorn config.wsgi:application --bind 0.0.0.0:${PORT:-8000} --workers 2 --threads 2"]
