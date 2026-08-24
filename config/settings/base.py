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

# The first six are the tenancy, auth and credential substrate (issue #31);
# the Layer-2 domain apps follow it.
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
    "apps.contacts",
    "apps.channels",
    "apps.messaging",
    "apps.media_library",
    "apps.flows",
    "apps.campaigns",
    "apps.broadcasts",
    "apps.inbox",
    "apps.notifications",
    "apps.queueing",
    "apps.api",
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
# Outbound send rate (SPEC §8, §20) — apps.messaging.buckets
# ---------------------------------------------------------------------------
# Per-platform token buckets default to PlatformPolicy.rate_default (telegram
# 25/s, instagram 8, messenger 40, whatsapp 20, sms 1, email 10). This env var
# overrides them per platform, which a self-hoster needs because the ceilings
# are per app, per page or per number and Meta hands them out individually:
#   DEFAULT_SEND_RATE_OVERRIDES={"telegram": 10, "sms": 0.5}
# Unknown platform keys and non-positive values fail a Django system check
# rather than being silently ignored — see apps/messaging/checks.py.
DEFAULT_SEND_RATE_OVERRIDES: dict[str, float] = env.json("DEFAULT_SEND_RATE_OVERRIDES", default={})

# How much burst a bucket holds, in seconds of its own rate. One second means a
# connection idle for a while may send one second's worth at once, which is what
# every platform's published limit already permits.
SEND_BUCKET_BURST_SECONDS = env.float("SEND_BUCKET_BURST_SECONDS", default=1.0)

# How long a worker will wait for a token before handing the wait to the queue.
# Read apps/messaging/buckets.py before raising it: the wait happens with the
# worker's transaction open.
SEND_BUCKET_MAX_WAIT_SECONDS = env.float("SEND_BUCKET_MAX_WAIT_SECONDS", default=2.0)

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
# Webhook ingestion (issue #4; SPEC §7.1, SECURITY-BASELINE §§2, 4, 7)
# ---------------------------------------------------------------------------
# /webhooks/ is the only unauthenticated write path in the product, so its
# limits are settings rather than constants: a deployment behind a platform
# that batches unusually large deliveries can raise the cap without a fork,
# and one under attack can tighten the throttle without a deploy.
#
# The body cap is checked from Content-Length *before* the body is read, and
# is deliberately far below DATA_UPLOAD_MAX_MEMORY_SIZE (2.5 MB): real webhook
# deliveries are single-digit kilobytes.
WEBHOOK_MAX_BODY_BYTES = env.int("WEBHOOK_MAX_BODY_BYTES", default=256 * 1024)

# Nesting cap, applied to the raw bytes before json.loads ever sees them —
# Python's JSON parser recurses, so a nesting bomb is a stack overflow rather
# than a catchable exception.
WEBHOOK_MAX_JSON_DEPTH = env.int("WEBHOOK_MAX_JSON_DEPTH", default=20)

# Signature-failure throttle. A correctly configured platform never fails a
# signature check, so any failure is a misconfiguration (fixed once) or someone
# guessing a secret. Counted per client address and per connection; crossing
# the limit bans the source for WEBHOOK_SIGNATURE_BAN_SECONDS.
WEBHOOK_SIGNATURE_FAILURE_LIMIT = env.int("WEBHOOK_SIGNATURE_FAILURE_LIMIT", default=10)
WEBHOOK_SIGNATURE_FAILURE_WINDOW_SECONDS = env.int("WEBHOOK_SIGNATURE_FAILURE_WINDOW_SECONDS", default=300)
WEBHOOK_SIGNATURE_BAN_SECONDS = env.int("WEBHOOK_SIGNATURE_BAN_SECONDS", default=900)

# How long raw webhook events are kept (SPEC §5). This is also the replay
# protection window: dedup is the unique constraint on the event log, so an
# event whose row has been pruned can be replayed with its original signature.
# See apps.channels.models.WebhookEventLog.
WEBHOOK_EVENT_LOG_RETENTION_DAYS = env.int("WEBHOOK_EVENT_LOG_RETENTION_DAYS", default=30)

# ---------------------------------------------------------------------------
# Outbound HTTP to user-supplied URLs (issue #15; SPEC §11.7, SECURITY-BASELINE §6)
# ---------------------------------------------------------------------------
# Consumed by apps.common.outbound.guarded_request, which is the ONLY path in
# this project allowed to fetch a URL a user, flow author or contact supplied.
#
# The guard denies loopback, link-local (the cloud metadata service),
# multicast, reserved and unspecified addresses, and the deployment's own host,
# always. This flag relaxes the *private-range* rule alone (RFC1918,
# fc00::/7), which an on-prem deployment whose partner services live on
# 10.0.0.0/8 genuinely needs. Turning it on does not open loopback or the
# metadata service — see apps/common/outbound.py for why the order of those
# checks is load-bearing.
EXTERNAL_REQUEST_ALLOW_PRIVATE = env.bool("EXTERNAL_REQUEST_ALLOW_PRIVATE", default=False)

# Whether an email channel may relay through a host on this machine or this
# private network (issue #21). Its own flag rather than the one above, because
# the two answer genuinely different questions.
#
# For an HTTP integration, loopback is never a legitimate target — it is a
# documented SSRF payload and nothing else, which is why the guard denies it
# whatever EXTERNAL_REQUEST_ALLOW_PRIVATE says. For SMTP it is the opposite: a
# local postfix, or a relay sidecar in the same compose file, is one of the
# commonest mail setups there is.
#
# So the default stays closed — on a multi-tenant install `manage_channels` is a
# *workspace* permission, and an unguarded SMTP host field is an internal port
# scanner with a form in front of it — and a single-tenant deployment that
# relays locally turns this on deliberately.
EMAIL_SMTP_ALLOW_INTERNAL = env.bool("EMAIL_SMTP_ALLOW_INTERNAL", default=False)

# How much of a response body the guard will read, with a streaming cutoff. A
# flow variable is not a place to put a megabyte, and the request runs with the
# contact's advisory lock held.
EXTERNAL_REQUEST_MAX_RESPONSE_BYTES = env.int("EXTERNAL_REQUEST_MAX_RESPONSE_BYTES", default=1024 * 1024)

# The same question for a contact's inbound attachment (apps/channels/media.py),
# and a separate knob because the answer is genuinely different: the setting
# above sizes a JSON body bound for a flow variable, this one sizes a
# photograph. Its own name rather than a hard-coded argument at the call site,
# so an operator hardening a deployment can see and change it — passing
# `max_bytes=` to the guard overrides EXTERNAL_REQUEST_MAX_RESPONSE_BYTES, and a
# cap that silently ignores the operator's is worse than no cap at all.
#
# It is an allocation bound, not a bandwidth one: the guard buffers what it
# reads, so this times the number of concurrent readers is memory the web
# process will hold. Four request slots ship by default (see the Procfile).
INBOUND_MEDIA_MAX_BYTES = env.int("INBOUND_MEDIA_MAX_BYTES", default=16 * 1024 * 1024)

# ---------------------------------------------------------------------------
# Public REST API v1 and outbound webhooks (issue #25; SPEC §17)
# ---------------------------------------------------------------------------
# Request body cap for /api/v1/, enforced before the JSON parser and before any
# database work (SECURITY-BASELINE §7). Matches WEBHOOK_MAX_BODY_BYTES above for
# the same reason: an API call that legitimately needs more than a quarter of a
# megabyte of JSON is not a shape this API has.
API_MAX_BODY_BYTES = env.int("API_MAX_BODY_BYTES", default=256 * 1024)

# Nesting cap on request bodies, applied to the raw bytes before json.loads.
API_MAX_JSON_DEPTH = env.int("API_MAX_JSON_DEPTH", default=20)

# SPEC §17's 10 req/s per key, counted by apps.common.ratelimit's Postgres
# fixed-window limiter. One second is the whole window, so Retry-After is
# always 1 and is truthful rather than a guess. Per key, so one integration
# cannot starve another.
API_RATE_LIMIT_PER_SECOND = env.int("API_RATE_LIMIT_PER_SECOND", default=10)

# Failed-bearer throttle, per client address. A correct integration never fails
# auth, so repeated failures are a misconfiguration or someone guessing keys;
# checked before the HMAC so a guessing script does not get to pay only the
# hash cost per attempt.
API_AUTH_FAILURE_LIMIT = env.int("API_AUTH_FAILURE_LIMIT", default=20)
API_AUTH_FAILURE_WINDOW_SECONDS = env.int("API_AUTH_FAILURE_WINDOW_SECONDS", default=300)

# Wall clock for one outbound webhook delivery, passed to guarded_request as its
# total deadline. Deliveries run on the worker, not in a request, but a slow
# receiver still holds a queue slot.
API_WEBHOOK_TIMEOUT_SECONDS = env.int("API_WEBHOOK_TIMEOUT_SECONDS", default=10)

# The "send test event" button is the one delivery that runs inside a request
# rather than on the worker, because an operator clicking Test wants the answer
# and not a "queued" toast. That makes its deadline a web-tier concern: it is
# time a gunicorn thread spends unavailable, so it is deliberately shorter than
# the worker's. A receiver that cannot answer in three seconds has told the
# operator what they needed to know.
#
# It bounds the HTTP phase only. DNS resolution sits outside guarded_request's
# deadline (see its module docstring), so a hostname whose resolver black-holes
# queries can still hold the thread for the system resolver's own timeout.
API_WEBHOOK_TEST_TIMEOUT_SECONDS = env.int("API_WEBHOOK_TEST_TIMEOUT_SECONDS", default=3)

# Tolerance a receiver is told to allow on X-BrightBean-Timestamp, published in
# docs/api/v1.md. We do not enforce it — the receiver does — but the number has
# to be written down somewhere both the docs and the tests can read.
API_WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS = env.int("API_WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS", default=300)

# Consecutive failed deliveries before an endpoint is switched off and its admins
# notified (SPEC §17). A "failure" is one delivery that exhausted its queue
# retries, not one HTTP attempt: the queue already retries five times on the
# standard backoff, so 100 here is roughly a day and a half of a dead receiver.
API_WEBHOOK_MAX_CONSECUTIVE_FAILURES = env.int("API_WEBHOOK_MAX_CONSECUTIVE_FAILURES", default=100)

# Delivery-log rows kept per webhook; the settings page shows this many.
API_WEBHOOK_DELIVERY_LOG_KEEP = env.int("API_WEBHOOK_DELIVERY_LOG_KEEP", default=50)

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
    # `or "auto"` because a *blank* value is not an absent one. environ.Env
    # returns the default only when the variable is unset, and every one-click
    # deploy target sets an empty config var for a prompt the operator left
    # blank — so `S3_REGION_NAME=` would reach boto3 as region_name="" and
    # build a malformed endpoint at first upload, instead of the documented
    # default this line already names.
    AWS_S3_REGION_NAME = env("S3_REGION_NAME", default="auto") or "auto"
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

# ---------------------------------------------------------------------------
# Media library limits (SECURITY-BASELINE §9, issue #16)
# ---------------------------------------------------------------------------
# Studio resolves its storage cap through subscription tiers and an override
# row. This is a self-hostable product, so the limits are environment variables
# with defaults that fit a small box, and the boundary they are counted against
# is the workspace — the same tenant boundary everything else is scoped to.
#
# Two independent limits because they stop different things: the per-file cap
# bounds what one request can cost, the per-workspace cap bounds what a member
# can accumulate over a thousand of them.
_MB = 1024 * 1024
MEDIA_MAX_UPLOAD_BYTES_IMAGE = env.int("MEDIA_MAX_UPLOAD_BYTES_IMAGE", default=20 * _MB)
MEDIA_MAX_UPLOAD_BYTES_AUDIO = env.int("MEDIA_MAX_UPLOAD_BYTES_AUDIO", default=50 * _MB)
MEDIA_MAX_UPLOAD_BYTES_VIDEO = env.int("MEDIA_MAX_UPLOAD_BYTES_VIDEO", default=200 * _MB)
MEDIA_MAX_UPLOAD_BYTES_FILE = env.int("MEDIA_MAX_UPLOAD_BYTES_FILE", default=25 * _MB)
MEDIA_WORKSPACE_QUOTA_BYTES = env.int("MEDIA_WORKSPACE_QUOTA_BYTES", default=5 * 1024 * _MB)
MEDIA_MAX_FILES_PER_UPLOAD = env.int("MEDIA_MAX_FILES_PER_UPLOAD", default=20)
# Folders nest at most three deep but nothing bounded how WIDE a library could
# get, and three separate surfaces render the whole set unpaginated — the picker
# payload, the move dropdown and the sidebar rail. Capping creation bounds all
# three at once, which is the level the limit belongs at.
MEDIA_MAX_FOLDERS_PER_WORKSPACE = env.int("MEDIA_MAX_FOLDERS_PER_WORKSPACE", default=500)
MEDIA_THUMBNAIL_SIZE = (400, 400)
# Pillow decompression-bomb guard. A 10 KB PNG can declare 60000x60000 and cost
# gigabytes to expand; apps.media_library.thumbnails checks the declared size
# against this before any pixel data is decoded.
MEDIA_MAX_IMAGE_PIXELS = env.int("MEDIA_MAX_IMAGE_PIXELS", default=50_000_000)

# Django's own DATA_UPLOAD_MAX_NUMBER_FILES is deliberately left at its default
# of 100. Pinning it to MEDIA_MAX_FILES_PER_UPLOAD would impose one app's batch
# size on every multipart endpoint in the project, so a later bulk importer
# would be refused by the framework with an error naming neither itself nor the
# media library. The media cap is enforced where it means something — the upload
# view — and Django's default still bounds the absurd case.

# ---------------------------------------------------------------------------
# Contact CSV import (SECURITY-BASELINE §7, issue #13)
# ---------------------------------------------------------------------------
# The issue's acceptance criterion is "50k-row CSV imports in background without
# web-request timeouts", so the row cap is the number the product promises and
# the batch size is what keeps one queued action — and therefore one database
# transaction (apps.queueing.worker) — short.
#
# The byte cap is the one that actually stops an attack: rows are bounded by it
# whatever CONTACT_IMPORT_MAX_ROWS says, and it is checked against the upload's
# size before a single row is parsed.
CONTACT_IMPORT_MAX_BYTES = env.int("CONTACT_IMPORT_MAX_BYTES", default=20 * _MB)
CONTACT_IMPORT_MAX_ROWS = env.int("CONTACT_IMPORT_MAX_ROWS", default=50_000)
CONTACT_IMPORT_BATCH_ROWS = env.int("CONTACT_IMPORT_BATCH_ROWS", default=500)
# Rows shown in the wizard's inline preview. Read synchronously in the request,
# so it is small on purpose; the full-file check is the queued dry run.
CONTACT_IMPORT_PREVIEW_ROWS = env.int("CONTACT_IMPORT_PREVIEW_ROWS", default=20)
# How long a finished import's uploaded file is kept. The file is a spreadsheet
# of personal data whose only remaining purpose is the report beside it, so the
# default is short and the housekeeping job in apps.contacts.housekeeping drops
# the file while leaving the run's counters and row errors readable.
CONTACT_IMPORT_FILE_RETENTION_DAYS = env.int("CONTACT_IMPORT_FILE_RETENTION_DAYS", default=30)

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
