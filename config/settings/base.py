"""Base settings shared by every environment.

Layout, env handling and the storage switch follow BrightBean Studio's
``config/settings/base.py``. Differences from Studio are marked inline.
"""

import os
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
    # Required by django-allauth even though the Google provider is configured
    # from settings rather than a SocialApp row: allauth builds absolute URLs
    # and email copy from the current Site.
    "django.contrib.sites",
]

THIRD_PARTY_APPS = [
    "csp",
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "allauth.socialaccount.providers.google",
]

# The first six are the tenancy, auth and credential substrate (issue #31) plus
# the Layer-2 task queue (issue #5); the rest of the domain apps follow.
#
# ``theme`` holds the compiled Tailwind bundle. It has to be an installed app
# rather than a STATICFILES_DIRS entry, because that is what puts
# theme/static/ in front of the app-directories finder and makes
# {% static 'css/dist/styles.css' %} resolve. There is deliberately no
# django-tailwind dependency and no TAILWIND_APP_NAME: Studio carries both and
# never invokes `manage.py tailwind`, so the npm scripts in package.json are
# the real build (deviation 1 of the L1-B brief).
LOCAL_APPS = [
    "apps.common",
    "apps.accounts",
    "apps.organizations",
    "apps.workspaces",
    "apps.members",
    "apps.credentials",
    "apps.queueing",
    "theme",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    # Before sessions and CSRF on purpose: a credential-stuffing spray with no
    # CSRF token should still burn its budget rather than getting a free 403.
    "apps.accounts.middleware.AuthRateLimitMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # allauth requires this immediately after AuthenticationMiddleware.
    "allauth.account.middleware.AccountMiddleware",
    # Reads request.user, so it has to come after authentication. Resolves the
    # workspace named by the URL and 404s the ones the user cannot reach.
    "apps.members.middleware.RBACMiddleware",
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
                "apps.accounts.context_processors.auth_providers",
                # Supplies the sidebar navigation with its `active` flag already
                # computed — the single active-state convention the whole UI
                # uses (deviation 4). Returns {} for anonymous requests, so the
                # auth pages cost nothing.
                "apps.common.context_processors.sidebar_context",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# Cache. Postgres is the only datastore (SPEC §2: no Redis, ever) and it is
# also the rate limiter (SPEC §22).
#
# The default is the DATABASE cache, not LocMemCache, because LocMemCache is per
# process and both the Dockerfile and the Procfile run gunicorn with two
# workers: anything counted there is counted once per worker and is evaded by
# landing on the other one. A per-process default is a trap for the next thing
# that needs to count across requests, so the shared backend is the default and
# the single-process case is the override. The table is created by
# apps/common/migrations/0001_cache_table.py, so `manage.py migrate` is enough
# and there is no second deploy step to forget.
#
# Auth rate limiting does NOT use this. Django's cache API has no atomic
# increment on a database backend — DatabaseCache inherits BaseCache.incr, which
# is a get followed by a set — so counting through it loses attempts under
# concurrency. apps/common/ratelimit.py counts in a row under select_for_update
# instead (SPEC §22: "Postgres is ... the rate limiter").
CACHES = {
    "default": env.cache("CACHE_URL", default="dbcache://cache_table"),
}

# Database
DATABASES = {
    "default": env.db("DATABASE_URL", default="postgres://postgres:postgres@localhost:5432/brightbean_chat"),
}

# ---------------------------------------------------------------------------
# Authentication (SECURITY-BASELINE §8)
# ---------------------------------------------------------------------------
AUTH_USER_MODEL = "accounts.User"

AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 8}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
]

# bcrypt(sha256) first, PBKDF2 retained so existing hashes still verify and are
# upgraded transparently. Requires the `bcrypt` package (requirements.in).
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.BCryptSHA256PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
]

# Sites framework. allauth reads the current Site for email copy; the domain is
# set from APP_URL by a data migration in apps/accounts.
SITE_ID = 1

# ---------------------------------------------------------------------------
# django-allauth
# ---------------------------------------------------------------------------
# 65.x spellings: ACCOUNT_LOGIN_METHODS / ACCOUNT_SIGNUP_FIELDS replaced the old
# ACCOUNT_AUTHENTICATION_METHOD / ACCOUNT_EMAIL_REQUIRED pair.
ACCOUNT_LOGIN_METHODS = {"email"}
ACCOUNT_SIGNUP_FIELDS = ["email*", "password1*"]
ACCOUNT_USER_MODEL_USERNAME_FIELD = None
ACCOUNT_ADAPTER = "apps.accounts.adapters.AccountAdapter"
ACCOUNT_EMAIL_SUBJECT_PREFIX = "[BrightBean Chat] "

# "optional", not Studio's "none" and not "mandatory". A verification email goes
# out at signup, and nothing is ever gated on it — so a self-hoster who has not
# configured SMTP is never locked out of their own instance, while a deployment
# that has configured it gets the audit trail. The account adapter additionally
# swallows send failures, because an SMTPException inside signup would lock them
# out just as hard.
ACCOUNT_EMAIL_VERIFICATION = "optional"

LOGIN_REDIRECT_URL = "/"
ACCOUNT_LOGOUT_REDIRECT_URL = "/accounts/login/"

# The Google app is configured here rather than in a SocialApp database row, so
# a self-hoster sets two env vars instead of clicking through the admin.
GOOGLE_AUTH_CLIENT_ID = env("GOOGLE_AUTH_CLIENT_ID", default="")
GOOGLE_AUTH_CLIENT_SECRET = env("GOOGLE_AUTH_CLIENT_SECRET", default="")

SOCIALACCOUNT_PROVIDERS = {
    "google": {
        "APP": {"client_id": GOOGLE_AUTH_CLIENT_ID, "secret": GOOGLE_AUTH_CLIENT_SECRET},
        "SCOPE": ["profile", "email"],
        "AUTH_PARAMS": {"access_type": "online"},
        "VERIFIED_EMAIL": True,
    },
}
SOCIALACCOUNT_AUTO_SIGNUP = True
# Provider flows must be initiated by POST: a GET-initiated login is
# CSRF-forgeable, which is how an attacker logs a victim into the attacker's
# account.
SOCIALACCOUNT_LOGIN_ON_GET = False
SOCIALACCOUNT_ADAPTER = "apps.accounts.adapters.SocialAccountAdapter"

# Studio sets SOCIALACCOUNT_EMAIL_AUTHENTICATION and
# ..._AUTO_CONNECT, which silently link a Google login to any existing local
# account with the same address. Deliberately NOT ported. Studio can afford it;
# we cannot, because ACCOUNT_EMAIL_VERIFICATION is "optional" here, so local
# accounts exist with unverified addresses — and auto-connect then reads as:
# register locally as victim@example.com, wait for the victim's first Google
# login, inherit their session. Unlinked Google logins go through allauth's
# ordinary signup/connect flow instead.

# ---------------------------------------------------------------------------
# Background task queue (SPEC §15)
# ---------------------------------------------------------------------------
# The shared secret for /internal/tick, the HTTP wrapper around one worker
# cycle. It exists for hosts with no always-on process: a cron service or an
# uptime pinger calls the URL every minute and that is the whole scheduler.
#
# Blank — the default — means the route 404s, which is the right posture for the
# deployments that run `manage.py process_tasks` instead and have no use for it.
# Being a plain shared secret rather than a signed token is deliberate and
# argued in apps/queueing/views.py: the caller is a third-party pinger holding
# one static URL forever, so there is no expiry to sign in.
TICK_TOKEN = env("TICK_TOKEN", default="")

# ---------------------------------------------------------------------------
# Reverse proxies (consumed by apps.common.net.get_client_ip)
# ---------------------------------------------------------------------------
# Which peers are allowed to speak for someone else via X-Forwarded-For.
# Defaults to nothing: the header is attacker-controlled unless a proxy you own
# wrote it, and trusting it by default turns the auth rate limiter off.
# Accepts addresses or CIDR ranges, e.g. "10.0.0.0/8,127.0.0.1".
#
# Orthogonal to development.py's SECURE_PROXY_SSL_HEADER / USE_X_FORWARDED_HOST:
# those decide the request's scheme and host for tunnelled webhook development.
# This decides the *client's identity* for rate limiting, where being wrong
# disables a security control rather than breaking a redirect. Behind a tunnel,
# set TRUSTED_PROXIES=127.0.0.1 to get per-caller buckets back.
TRUSTED_PROXIES = env.list("TRUSTED_PROXIES", default=[])

# ---------------------------------------------------------------------------
# Deployment-level platform credentials — the bottom of the SPEC §4 chain
# ---------------------------------------------------------------------------
# PLATFORM_<PLATFORM>_<KEY> in the environment, e.g.
#   PLATFORM_INSTAGRAM_CLIENT_ID / PLATFORM_INSTAGRAM_CLIENT_SECRET
# becomes {"instagram": {"client_id": ..., "client_secret": ...}}.
#
# The slugs are duplicated from apps.common.platforms.Platform rather than
# imported, because a settings module must not import model code — the app
# registry does not exist yet. apps.common.checks.check_platform_env_slugs fails
# the build if the two ever disagree.
PLATFORM_ENV_SLUGS = ("telegram", "instagram", "messenger", "whatsapp", "sms", "email")


def _platform_credentials_from_env() -> dict[str, dict[str, str]]:
    """Group PLATFORM_* environment variables by platform.

    Longest slug first so a future two-word platform cannot be mis-split by a
    shorter one that happens to be a prefix.
    """
    collected: dict[str, dict[str, str]] = {}
    for slug in sorted(PLATFORM_ENV_SLUGS, key=len, reverse=True):
        prefix = f"PLATFORM_{slug.upper()}_"
        for name, value in os.environ.items():
            if not name.startswith(prefix) or not value.strip():
                continue
            collected.setdefault(slug, {})[name[len(prefix) :].lower()] = value.strip()
    return collected


PLATFORM_CREDENTIALS_FROM_ENV = _platform_credentials_from_env()

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
