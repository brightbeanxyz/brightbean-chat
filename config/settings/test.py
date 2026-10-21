import hashlib
import os

# Set before importing base: base.py refuses to boot with DEBUG=False and no
# secrets, and the test settings deliberately run with DEBUG=False so tests
# exercise the production code paths. (Studio uses the same ordering trick.)
# Forced, not setdefault: the suite asserts production-path behaviour, so a
# developer with DEBUG=true exported must not silently get the DEBUG branch.
os.environ["DEBUG"] = "false"

os.environ.setdefault("SECRET_KEY", "test-secret-key-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY_SALT", "test-salt-not-for-production")
os.environ.setdefault("ALLOWED_HOSTS", "*")

from .base import *  # noqa: E402, F401, F403

DEBUG = False
ALLOWED_HOSTS = ["*"]

# ---------------------------------------------------------------------------
# One test database per checkout
# ---------------------------------------------------------------------------
#
# Without this the test database is named after DATABASE_URL's database, which
# every checkout inherits verbatim from .env.example — so every worktree on a
# machine shares one `test_brightbean_chat`. Two pytest sessions then run
# against the same rows, and the failures that produces do not name their cause:
# `claim_batch` is cross-tenant by design (apps/queueing/worker.py), so it
# cheerfully claims another session's committed `scheduled_action` rows and the
# batch-limit assertions in apps/queueing/tests see more rows than they created;
# the shared token buckets defer sends that should have gone straight out; and
# the loser of a database-creation race gets a bare `DuplicateDatabase` several
# screens from anything it did. All of it reads as a regression in whatever the
# developer happened to be editing, which is the expensive part.
#
# Derived from the checkout path rather than configured, because an isolation
# mechanism that needs a per-worktree .env edit to work is one that is off by
# default. CI has a single checkout, so the suffix is stable there and the name
# behaves exactly as before.
#
# The base name is truncated to keep the whole identifier inside Postgres's
# 63-byte limit, because Postgres truncates from the *right* — which would take
# the suffix off and hand two worktrees the same database again, silently, which
# is the failure this block exists to prevent.
_CHECKOUT_SUFFIX = hashlib.blake2s(str(BASE_DIR).encode(), digest_size=4).hexdigest()  # noqa: F405
_MAX_IDENTIFIER_BYTES = 63
_NAME_BUDGET = _MAX_IDENTIFIER_BYTES - len("test_") - len("_") - len(_CHECKOUT_SUFFIX)
DATABASES["default"].setdefault("TEST", {})["NAME"] = (  # noqa: F405
    f"test_{DATABASES['default']['NAME'][:_NAME_BUDGET]}_{_CHECKOUT_SUFFIX}"  # noqa: F405
)

# Faster password hasher in tests.
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.MD5PasswordHasher",
]

EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

# Concrete models used only to exercise apps.common (BaseModel, encrypted
# fields). Never installed outside the test settings.
INSTALLED_APPS = [*INSTALLED_APPS, "tests.testapp"]  # noqa: F405

STORAGE_BACKEND = "local"
MEDIA_ROOT = BASE_DIR / "test_media"  # noqa: F405

# Simple static files storage in tests (no manifest/collectstatic needed).
STORAGES["staticfiles"] = {  # noqa: F405
    "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
}

# WhiteNoise scans STATIC_ROOT at startup and warns once per test module when
# it is missing. Tests never run collectstatic, so let it resolve files lazily
# instead of filling the CI log with a warning about an expected absence.
WHITENOISE_AUTOREFRESH = True

# Cookies cannot be Secure over the test client's plain HTTP requests.
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
