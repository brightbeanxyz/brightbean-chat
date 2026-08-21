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

from collections.abc import Callable

from django.http import HttpRequest, HttpResponse

from apps.common.net import get_client_ip
from apps.common.ratelimit import hit, window_key

# Prefix-matched. allauth mounts these under /accounts/.
AUTH_RATE_LIMITED_PATHS = (
    "/accounts/login/",
    "/accounts/signup/",
    "/accounts/password/reset/",
)

AUTH_RATE_LIMIT = 10
AUTH_RATE_WINDOW = 60  # seconds

RATE_LIMIT_NAMESPACE = "auth"


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

    @staticmethod
    def _over_limit(request: HttpRequest) -> bool:
        key = window_key(
            RATE_LIMIT_NAMESPACE,
            get_client_ip(request),
            window_seconds=AUTH_RATE_WINDOW,
        )
        return hit(key, limit=AUTH_RATE_LIMIT, window_seconds=AUTH_RATE_WINDOW)
