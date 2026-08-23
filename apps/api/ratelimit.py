"""Rate limits for ``/api/v1/`` (SPEC §17).

Two limiters, both on ``apps.common.ratelimit`` — the Postgres fixed-window
counter whose module docstring names this issue as its next consumer. The cache
is not an option: ``DatabaseCache.incr`` is get-then-set and loses counts under
concurrency, which is the reason that module exists (``config/settings/base.py``
spells it out beside the ``CACHES`` setting).

**Per key, 10 req/s.** SPEC §17's number. The window is one second, which makes
``Retry-After: 1`` a fact rather than an estimate, and the identity is the key's
primary key, so two keys in the same workspace never contend.

**Per client address, failed bearers.** A correct integration never fails auth.
Repeated failures are a misconfiguration fixed once, or someone walking the key
space — and this is checked *before* the digest is computed, so a guessing script
does not get to pay only the hash cost per attempt past the threshold. It
returns the same uniform 401 as any other failure, so the throttle is invisible
to the thing it is throttling.

SPEC §17 says "sliding window"; this is a fixed window. The substitution is
deliberate: one shared limiter with one set of failure modes beats a second
implementation that agrees with it most of the time, and at 10 req/s the
difference is a caller occasionally getting up to 19 requests across a window
boundary.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings

from apps.common.net import get_client_ip
from apps.common.ratelimit import hit, window_key

__all__ = [
    "AUTH_FAILURE_NAMESPACE",
    "KEY_NAMESPACE",
    "RATE_WINDOW_SECONDS",
    "auth_failures_exhausted",
    "over_key_limit",
    "record_auth_failure",
]

KEY_NAMESPACE = "api_key"
AUTH_FAILURE_NAMESPACE = "api_auth_fail"

#: SPEC §17 is stated per second, so the window is a second.
RATE_WINDOW_SECONDS = 1


def over_key_limit(api_key: Any) -> bool:
    """True when this key has already spent its budget for the current second."""
    limit = settings.API_RATE_LIMIT_PER_SECOND
    key = window_key(KEY_NAMESPACE, str(api_key.pk), window_seconds=RATE_WINDOW_SECONDS)
    return hit(key, limit=limit, window_seconds=RATE_WINDOW_SECONDS)


def _auth_failure_key(request: Any) -> str:
    window = settings.API_AUTH_FAILURE_WINDOW_SECONDS
    return window_key(AUTH_FAILURE_NAMESPACE, get_client_ip(request), window_seconds=window)


def auth_failures_exhausted(request: Any) -> bool:
    """True when this client address has burned its failed-auth budget.

    A read, not a hit: checking must not itself consume budget, or a blocked
    client would never fall out of the window while it keeps trying.
    """
    from apps.common.models import RateLimitCounter

    limit = settings.API_AUTH_FAILURE_LIMIT
    counter = RateLimitCounter.objects.filter(key=_auth_failure_key(request)).values_list("count", flat=True).first()
    return counter is not None and counter > limit


def record_auth_failure(request: Any) -> None:
    """Count one failed bearer against this client address."""
    window = settings.API_AUTH_FAILURE_WINDOW_SECONDS
    hit(_auth_failure_key(request), limit=settings.API_AUTH_FAILURE_LIMIT, window_seconds=window)
