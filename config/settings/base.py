"""Base settings shared by every environment.

Layout, env handling and the storage switch follow BrightBean Studio's
``config/settings/base.py``. Differences from Studio are marked inline.
"""

from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import environ
from csp.constants import NONCE, NONE, SELF, UNSAFE_EVAL, UNSAFE_INLINE
from django.core.exceptions import ImproperlyConfigured

from apps.common.placeholders import is_placeholder_secret

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env(
    DEBUG=(bool, False),
    ALLOWED_HOSTS=(list, []),
    APP_URL=(str, "http://localhost:8000"),
    STORAGE_BACKEND=(str, "local"),
    EMAIL_BACKEND_TYPE=(str, "smtp"),
    SENTRY_DSN=(str, ""),
)

# ``DJANGO_ENV_FILE`` lets a deployment (or a test) point at a different file,
# or at a path that does not exist to guarantee a pristine environment.
environ.Env.read_env(env.str("DJANGO_ENV_FILE", default=str(BASE_DIR / ".env")), overwrite=False)

DEBUG = env("DEBUG")
ALLOWED_HOSTS = env("ALLOWED_HOSTS")
APP_URL = env("APP_URL")

# ---------------------------------------------------------------------------
# Secrets — enforced at boot, not lazily (SECURITY-BASELINE §8)
# ---------------------------------------------------------------------------
# Studio reads SECRET_KEY eagerly but validates ENCRYPTION_KEY_SALT lazily,
# inside the first encrypted-field access. A deployment missing the salt
# therefore boots green and only breaks when the first webhook tries to read a
# credential. Here both are checked while settings are being imported, so a
# misconfigured deploy fails immediately and visibly.
#
# This runs at import time, off the DEBUG value in the environment, so a
# settings module that means production must say so BEFORE importing this one —
# see config/settings/production.py. apps.common.checks re-validates the
# fully-loaded settings as a backstop, because an import-order mistake here is
# silent and expensive.
#
# ALLOWED_HOSTS is checked here too. It is the likeliest thing to forget, and
# forgetting it is invisible at boot: Django starts happily and then 400s every
# request, including /healthz, so the container healthcheck fails with a
# generic status and nothing says why.
#
# DEBUG deployments get throwaway defaults so `manage.py` works on a fresh
# clone with no .env; anything else must supply real values.
# Exported (no leading underscore) so apps.common.checks can recognise them in
# the fully-loaded settings and refuse to run on them outside DEBUG.
DEV_INSECURE_SECRET_KEY = "django-insecure-dev-only-do-not-use-in-production"  # noqa: S105
DEV_INSECURE_ENCRYPTION_KEY_SALT = "django-insecure-dev-only-salt-not-for-production"

SECRET_KEY = env("SECRET_KEY", default="")
_ENCRYPTION_KEY_SALT = env("ENCRYPTION_KEY_SALT", default="")

if DEBUG:
    SECRET_KEY = SECRET_KEY or DEV_INSECURE_SECRET_KEY
    _ENCRYPTION_KEY_SALT = _ENCRYPTION_KEY_SALT or DEV_INSECURE_ENCRYPTION_KEY_SALT
else:
    _missing = [
        name
        for name, value in (
            ("SECRET_KEY", SECRET_KEY),
            ("ENCRYPTION_KEY_SALT", _ENCRYPTION_KEY_SALT),
        )
        if not value.strip()
    ]
    if not [host for host in ALLOWED_HOSTS if host.strip()]:
        _missing.append("ALLOWED_HOSTS")

    # A placeholder is as dangerous as a blank value and looks fine to the
    # "is it set?" test above: .env.example ships one, and `make setup` copies
    # that file verbatim.
    _placeholders = [
        name
        for name, value in (
            ("SECRET_KEY", SECRET_KEY),
            ("ENCRYPTION_KEY_SALT", _ENCRYPTION_KEY_SALT),
        )
        if is_placeholder_secret(value)
    ]

    if _missing or _placeholders:
        _hints = {
            "SECRET_KEY": 'generate one with: python -c "import secrets; print(secrets.token_urlsafe(50))"',
            "ENCRYPTION_KEY_SALT": "generate a second, different random value the same way",
            "ALLOWED_HOSTS": "comma-separated hostnames this deployment answers on, e.g. chat.example.com",
        }
        _problems = [f"  - {name} is not set: {_hints[name]}" for name in _missing]
        _problems += [
            f"  - {name} is still a placeholder, which is published in this repository: {_hints[name]}"
            for name in _placeholders
        ]
        raise ImproperlyConfigured(
            "Refusing to boot with DEBUG=False.\n" + "\n".join(_problems) + "\nSee docs/SECURITY-BASELINE.md §8."
        )

# Encryption key derivation salt — consumed by apps.common.encryption.
ENCRYPTION_KEY_SALT = _ENCRYPTION_KEY_SALT.encode("utf-8")

# Application definition

DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
]

THIRD_PARTY_APPS = [
    "csp",
]

# Business apps land in their own issues: tenancy in #31, theme in #32, the
# domain apps from Layer 2 onwards. Layer 0 ships apps.common and nothing else.
LOCAL_APPS = [
    "apps.common",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "csp.middleware.CSPMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# Cache. Postgres is the only datastore (SPEC §2: no Redis, ever), so the
# default is the local-memory backend.
#
# LocMemCache is PER PROCESS. Both the Dockerfile and the Procfile run gunicorn
# with two workers, so anything that counts across requests — the auth rate
# limiting SECURITY-BASELINE §8 requires of issue #31 above all — counts once
# per worker and is evaded by landing on the other one. Any deployment relying
# on such a counter must point CACHE_URL at a shared backend, e.g.
# "dbcache://cache_table" (then run `manage.py createcachetable`), which keeps
# the no-Redis rule while giving every worker one view of the data.
CACHES = {
    "default": env.cache("CACHE_URL", default="locmemcache://"),
}

# Database
DATABASES = {
    "default": env.db("DATABASE_URL", default="postgres://postgres:postgres@localhost:5432/brightbean_chat"),
}

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 8}},
]

# Internationalization
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# Static files
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
STORAGES = {
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

# Media files. One STORAGE_BACKEND switch, generic S3_* names so any
# S3-compatible endpoint (AWS, Cloudflare R2, MinIO) works unchanged.
#
# MEDIA_URL and MEDIA_ROOT are set for BOTH backends, not just local. Django
# normalises an unset MEDIA_URL to "/", and config/urls.py feeds MEDIA_URL to
# django.conf.urls.static.static() under DEBUG — with "/" that compiles to a
# catch-all ^(?P<path>.*)$ route serving the working directory, which turns
# every 404 into a filesystem read of the project (including .env).
MEDIA_URL = "/media/"
MEDIA_ROOT = env("MEDIA_ROOT", default=str(BASE_DIR / "media"))

STORAGE_BACKEND = env("STORAGE_BACKEND")
STORAGE_IS_LOCAL = STORAGE_BACKEND.lower() != "s3"
if not STORAGE_IS_LOCAL:
    STORAGES["default"] = {
        "BACKEND": "storages.backends.s3boto3.S3Boto3Storage",
    }
    AWS_S3_ENDPOINT_URL = env("S3_ENDPOINT_URL", default="")
    AWS_ACCESS_KEY_ID = env("S3_ACCESS_KEY_ID", default="")
    AWS_SECRET_ACCESS_KEY = env("S3_SECRET_ACCESS_KEY", default="")
    AWS_STORAGE_BUCKET_NAME = env("S3_BUCKET_NAME", default="")
    AWS_S3_CUSTOM_DOMAIN = env("S3_CUSTOM_DOMAIN", default="")
    AWS_S3_REGION_NAME = env("S3_REGION_NAME", default="auto")
    AWS_S3_FILE_OVERWRITE = False
    AWS_DEFAULT_ACL = "private"
    AWS_QUERYSTRING_AUTH = True
    AWS_QUERYSTRING_EXPIRE = 3600  # 1-hour expiry for presigned URLs
    AWS_S3_OBJECT_PARAMETERS = {
        "CacheControl": "max-age=86400",
    }
    # Signing for the custom-domain delivery path. django-storages only signs
    # custom-domain URLs when BOTH of these are set; without them a private
    # bucket behind S3_CUSTOM_DOMAIN hands out URLs that 403 (common.W001).
    AWS_CLOUDFRONT_KEY_ID = env("S3_CLOUDFRONT_KEY_ID", default="")
    AWS_CLOUDFRONT_KEY = env("S3_CLOUDFRONT_KEY", default="")
else:
    # Local FS fallback so dev + test environments without S3 credentials can
    # still call default_storage / save uploaded files.
    STORAGES["default"] = {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    }

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------------------
# Cookies and sessions (SECURITY-BASELINE §8)
# ---------------------------------------------------------------------------
# Secure defaults live here rather than only in production.py so that any new
# settings module inherits them; development.py is the single place that
# relaxes the Secure flag, because plain-HTTP localhost cannot set it.
SESSION_ENGINE = "django.contrib.sessions.backends.db"
SESSION_COOKIE_AGE = 14 * 24 * 60 * 60  # 14 days
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = True
SESSION_SAVE_EVERY_REQUEST = True  # Sliding window
# CSRF cookie is deliberately NOT HttpOnly: HTMX reads it to populate the
# X-CSRFToken header on non-GET requests.
CSRF_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SECURE = True

# Email
EMAIL_BACKEND_TYPE = env("EMAIL_BACKEND_TYPE")
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="noreply@localhost")

if EMAIL_BACKEND_TYPE == "smtp":
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    EMAIL_HOST = env("EMAIL_HOST", default="localhost")
    EMAIL_PORT = env.int("EMAIL_PORT", default=587)
    EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="")
    EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default="")
    EMAIL_USE_TLS = env.bool("EMAIL_USE_TLS", default=True)
else:
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# ---------------------------------------------------------------------------
# Content Security Policy (SECURITY-BASELINE §8)
# ---------------------------------------------------------------------------
# django-csp 4.x dict form. Studio is still on 3.x's module-level CSP_* names,
# which 4.0 removed; a greenfield project has no reason to adopt the retired
# spelling. Per-request nonces come from ``csp.constants.NONCE`` and are read
# in templates as ``{{ request.csp_nonce }}``.
#
# 'unsafe-eval' in script-src is required by Alpine.js's standard build, which
# evaluates x-* expressions at runtime. 'unsafe-inline' is confined to styles,
# where Tailwind utility classes make it unavoidable.
CSP_POLICY: dict[str, Any] = {
    "DIRECTIVES": {
        "default-src": [SELF],
        "script-src": [SELF, UNSAFE_EVAL, NONCE],
        "style-src": [SELF, UNSAFE_INLINE],
        "img-src": [SELF, "data:", "blob:", "https:"],
        "font-src": [SELF],
        "connect-src": [SELF],
        "media-src": [SELF, "blob:"],
        "frame-ancestors": [NONE],
        "form-action": [SELF],
        "base-uri": [SELF],
        "object-src": [NONE],
    },
}

# Development swaps this for the report-only header, hence the optional type.
CONTENT_SECURITY_POLICY: dict[str, Any] | None = CSP_POLICY


# Allow media/images from the storage domain when media lives off-origin.
def csp_origin(value: str) -> str | None:
    """Reduce a storage endpoint or custom domain to a CSP source expression.

    Keeps the scheme and the port, both of which matter. Studio prepends
    "https://" to anything not already starting with it, so an http:// MinIO
    endpoint becomes "https://http://localhost:9000"; and it builds the origin
    from ``.hostname``, dropping any non-default port, which yields a source
    that never matches the media URLs the page actually loads — the browser
    then blocks every image. Userinfo is dropped: it is not part of a CSP
    source, and it would be a credential in a response header.
    """
    value = value.strip()
    if not value:
        return None
    # A bare domain ("cdn.example.com") is the usual form for a custom domain.
    parsed = urlparse(value if "://" in value else f"https://{value}")
    if not parsed.hostname:
        return None
    try:
        port = parsed.port
    except ValueError:  # malformed port
        return None
    # .hostname strips the brackets an IPv6 literal needs to keep, and without
    # them the port is unparseable: "https://::1:9000" is not a valid source.
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    return f"{parsed.scheme}://{host}{f':{port}' if port else ''}"


if STORAGE_BACKEND.lower() == "s3":
    _storage_origin = csp_origin(AWS_S3_CUSTOM_DOMAIN or AWS_S3_ENDPOINT_URL)
    if _storage_origin:
        CSP_POLICY["DIRECTIVES"]["media-src"].append(_storage_origin)
        CSP_POLICY["DIRECTIVES"]["img-src"].append(_storage_origin)

# ---------------------------------------------------------------------------
# Logging (SECURITY-BASELINE §5)
# ---------------------------------------------------------------------------
# Studio defines LOGGING only in production.py, so its dev and test runs log
# through Django's defaults. The scrubbing filter has to be everywhere — a
# token leaked into a developer's terminal or a CI log is still leaked — so
# LOGGING lives in base.py and every environment inherits it. The filter is
# backed up by a global LogRecord factory installed in
# ``apps.common.apps.CommonConfig.ready()``, which also covers handlers this
# dict does not own (pytest's caplog, Sentry's breadcrumb handler, ...).
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "scrub_secrets": {
            "()": "apps.common.logging.SecretScrubbingFilter",
        },
    },
    "formatters": {
        "verbose": {
            "format": "{levelname} {asctime} {module} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
            "filters": ["scrub_secrets"],
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "WARNING",
    },
    "loggers": {
        "django": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
        "apps": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        # runserver's access log. Without an explicit entry it inherits the
        # "django" logger's WARNING level and every request line disappears —
        # a regression Django's own default LOGGING does not have, and one
        # this config would otherwise introduce by living in base.py rather
        # than production.py.
        "django.server": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "gunicorn.error": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
    },
}

# Sentry. Configured through apps.common.sentry so error reports get the same
# credential scrubbing as logs — Sentry builds events from exception objects
# and never touches the logging pipeline. See that module for the details.
SENTRY_DSN = env("SENTRY_DSN")
if SENTRY_DSN:
    from apps.common.sentry import configure_sentry

    configure_sentry(SENTRY_DSN)
