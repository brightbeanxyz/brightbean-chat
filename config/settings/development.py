import os

# Set before importing base: base.py refuses to boot without SECRET_KEY and
# ENCRYPTION_KEY_SALT unless DEBUG is on, and it reads DEBUG from the
# environment while building the rest of the settings. Flipping it here rather
# than after the import means a fresh clone with no .env still runs.
os.environ.setdefault("DEBUG", "true")

from .base import *  # noqa: E402, F401, F403

DEBUG = True
ALLOWED_HOSTS = ["*"]

# Tunnel-aware redirect handling. ngrok / cloudflared terminate TLS and forward
# plain HTTP to runserver; webhook development needs a public HTTPS origin from
# day one (SPEC §20), so trust the forwarded scheme and host.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True

# Django requires the request Origin / Referer host to be explicitly trusted for
# any POST arriving through a non-localhost hostname, even with DEBUG=True.
# Comma-separated, e.g. "https://foo.ngrok-free.app,https://bar".
CSRF_TRUSTED_ORIGINS = [o.strip() for o in env.list("CSRF_TRUSTED_ORIGINS", default=[]) if o.strip()]  # noqa: F405

# Plain storage in dev — no manifest needed, runserver uses finders directly.
STORAGES["staticfiles"] = {  # noqa: F405
    "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
}

EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# CSP in report-only mode locally so a new directive breaks the browser console
# rather than the page you are working on.
CONTENT_SECURITY_POLICY = None
CONTENT_SECURITY_POLICY_REPORT_ONLY = CSP_POLICY  # noqa: F405

# Plain HTTP on localhost cannot set Secure cookies; the browser would drop the
# session outright. Every other environment keeps the base.py default of True.
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False

# Django debug toolbar (optional — installed only if present)
try:
    import debug_toolbar  # noqa: F401

    INSTALLED_APPS += ["debug_toolbar"]  # noqa: F405
    MIDDLEWARE.insert(0, "debug_toolbar.middleware.DebugToolbarMiddleware")  # noqa: F405
    INTERNAL_IPS = ["127.0.0.1"]
except ImportError:
    pass
