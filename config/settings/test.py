import os

# Set before importing base: base.py refuses to boot with DEBUG=False and no
# secrets, and the test settings deliberately run with DEBUG=False so tests
# exercise the production code paths. (Studio uses the same ordering trick.)
os.environ.setdefault("SECRET_KEY", "test-secret-key-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY_SALT", "test-salt-not-for-production")
os.environ.setdefault("ALLOWED_HOSTS", "*")

from .base import *  # noqa: E402, F401, F403

DEBUG = False
ALLOWED_HOSTS = ["*"]

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
