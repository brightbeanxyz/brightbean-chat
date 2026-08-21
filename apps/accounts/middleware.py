"""Rate limiting on the authentication endpoints (SECURITY-BASELINE §8).

Ported from BrightBean Studio's ``AuthRateLimitMiddleware`` with three fixes.

**Trusted proxies.** Studio reads the leftmost ``X-Forwarded-For`` value
unconditionally, so any client can mint a fresh bucket per request and the
limiter stops existing. Client identity comes from
:func:`apps.common.net.get_client_ip`, which ignores the header unless the peer
is listed in ``TRUSTED_PROXIES`` (default: nothing is).

**Atomicity and window shape.** Studio does ``cache.get`` then ``cache.set``,
which loses concurrent increments, and its ``set`` refreshes the TTL on every
hit, so the "60-second window" slides. ``add`` + ``incr`` here gives a fixed
window that starts at the first attempt, and ``add`` is the atomic primitive the
cache API actually provides.

**Shared storage.** ``CACHE_URL`` defaults to the database cache, not
``LocMemCache``: gunicorn runs two workers, and a per-process counter is evaded
by landing on the other one. See ``config/settings/base.py``.

Enumeration safety comes for free — the limiter never reads the submitted
credentials, so an existing and a non-existent account are treated identically.
It is a per-IP volume control, not per-account: a distributed spray against one
account is out of scope here and belongs with account lockout.
"""

import hashlib
from collections.abc import Callable

from django.core.cache import cache
from django.http import HttpRequest, HttpResponse

from apps.common.net import get_client_ip

# Prefix-matched. allauth mounts these under /accounts/.
AUTH_RATE_LIMITED_PATHS = (
    "/accounts/login/",
    "/accounts/signup/",
    "/accounts/password/reset/",
)

AUTH_RATE_LIMIT = 10
AUTH_RATE_WINDOW = 60  # seconds

CACHE_KEY_PREFIX = "auth-ratelimit"


class AuthRateLimitMiddleware:
    """Cap unauthenticated POSTs to the auth endpoints, per client address."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        limited = request.method == "POST" and request.path.startswith(AUTH_RATE_LIMITED_PATHS)
        if limited and self._over_limit(request):
            response = HttpResponse("Too many requests. Please try again later.", status=429)
            # Without this a client has to guess when to come back, and every
            # well-behaved one guesses "immediately".
            response["Retry-After"] = str(AUTH_RATE_WINDOW)
            return response

        return self.get_response(request)

    def _over_limit(self, request: HttpRequest) -> bool:
        client_ip = get_client_ip(request)
        # Hashed to keep an IP address out of the cache table; it is a key
        # shortener and a privacy measure, not a security control.
        digest = hashlib.sha256(client_ip.encode("utf-8")).hexdigest()[:32]
        key = f"{CACHE_KEY_PREFIX}:{digest}"

        # add() only writes when the key is absent, so the TTL is set once and
        # the window is fixed rather than sliding.
        cache.add(key, 0, AUTH_RATE_WINDOW)
        try:
            attempts = cache.incr(key)
        except ValueError:
            # The key expired between add() and incr(). Treat it as the first
            # attempt of a new window rather than failing the request.
            cache.set(key, 1, AUTH_RATE_WINDOW)
            attempts = 1
        return attempts > AUTH_RATE_LIMIT
