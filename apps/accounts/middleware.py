"""Rate limiting on the authentication endpoints (SECURITY-BASELINE §8).

Ported from BrightBean Studio's ``AuthRateLimitMiddleware`` with three fixes.

**Trusted proxies.** Studio reads the leftmost ``X-Forwarded-For`` value
unconditionally, so any client can mint a fresh bucket per request and the
limiter stops existing. Client identity comes from
:func:`apps.common.net.get_client_ip`, which ignores the header unless the peer
is listed in ``TRUSTED_PROXIES`` (default: nothing is).

**A window that actually ends.** Studio does ``cache.get`` then ``cache.set``,
which loses concurrent increments and refreshes the TTL on every hit, so a
client under continuous load is never let back in. ``add`` + ``incr`` fixes the
first half. The second half needs the key to carry the window number, because
``BaseCache.incr`` is ``get`` + ``set`` *without* a timeout — it re-stamps the
entry with ``DEFAULT_TIMEOUT`` (five minutes), and a bucket keyed only on the
address would outlive its own window by four. Bucketing on
``now // AUTH_RATE_WINDOW`` makes the window start on the clock instead, so
``Retry-After`` is honest and the TTL only has to be long enough not to lose the
count mid-window.

A fixed window lets a client spend its allowance at the end of one window and
again at the start of the next. That is inherent to the shape and acceptable
here: this is a volume control against automated spraying, not an account
lockout.

**Shared storage.** ``CACHE_URL`` defaults to the database cache, not
``LocMemCache``: gunicorn runs two workers, and a per-process counter is evaded
by landing on the other one. See ``config/settings/base.py``.

Enumeration safety comes for free — the limiter never reads the submitted
credentials, so an existing and a non-existent account are treated identically.
It is a per-IP volume control, not per-account: a distributed spray against one
account is out of scope here.
"""

import hashlib
import time
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


def window_cache_key(client_ip: str, now: float | None = None) -> str:
    """The bucket for one address in the current window.

    The address is hashed to keep an IP out of the cache table; that is key
    shortening and privacy hygiene, not a security control.
    """
    window = int((time.time() if now is None else now) // AUTH_RATE_WINDOW)
    digest = hashlib.sha256(client_ip.encode("utf-8")).hexdigest()[:32]
    return f"{CACHE_KEY_PREFIX}:{window}:{digest}"


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
        key = window_cache_key(get_client_ip(request))

        # add() only writes when the key is absent, which is the atomic
        # primitive the cache API offers; incr() then counts without a
        # read-modify-write race between workers.
        cache.add(key, 0, AUTH_RATE_WINDOW * 2)
        try:
            attempts = cache.incr(key)
        except ValueError:
            # Evicted between add() and incr(). Treat it as the first attempt of
            # this window rather than failing the request.
            cache.set(key, 1, AUTH_RATE_WINDOW * 2)
            attempts = 1
        return attempts > AUTH_RATE_LIMIT
