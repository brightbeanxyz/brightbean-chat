"""A Postgres-backed fixed-window rate limiter.

SPEC §22: "No Redis ever in v1; Postgres is the queue, the lock manager, and
the rate limiter."

This does not go through Django's cache framework, and the reason is specific.
``DatabaseCache`` does not override ``incr``, so it inherits
``BaseCache.incr`` — a ``get`` followed by a ``set``. Two workers handling
simultaneous login POSTs read the same count and one write lands on top of the
other, so attempts are *lost*, not merely counted late: under the concurrency an
attacker actually generates, the limit stops being a limit. A cache backend that
offered an atomic increment would be fine; the one the no-Redis rule leaves us
with does not.

So the counter is a row, and the increment happens under
``select_for_update``. That serialises requests sharing a key, which on an auth
endpoint is the behaviour you want anyway — the contended case *is* the attack,
and everything else is already paying for a bcrypt hash.

The window is encoded in the key rather than tracked in the row, so a new window
is a new row and there is no expiry arithmetic on the read path. Issue #25's
per-API-key limit is the next consumer; it wants the same shape with a different
key and window.
"""

import hashlib
import logging
import time
from datetime import datetime, timedelta

from django.db import transaction
from django.utils import timezone

from apps.common.models import RateLimitCounter

logger = logging.getLogger(__name__)

__all__ = ["RateLimitCounter", "hit", "window_key"]


def window_key(namespace: str, identity: str, *, window_seconds: int, now: float | None = None) -> str:
    """A key that changes when the window does.

    Putting the window number in the key is what makes the window *fixed*: it
    starts on the clock rather than sliding forward with every attempt, so the
    ``Retry-After`` a caller is handed is the truth.
    """
    timestamp = time.time() if now is None else now
    window = int(timestamp // window_seconds)
    # Hashed so an IP address is not stored in a table that outlives the
    # request, and so the column length is bounded whatever the identity is.
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
    return f"{namespace}:{window}:{digest}"


def hit(key: str, *, limit: int, window_seconds: int) -> bool:
    """Record one attempt against ``key``; return True when it is over ``limit``.

    Exact, not approximate: the read and the increment happen inside one
    transaction holding a row lock, so no attempt is lost and the caller that
    crosses the threshold is the one that gets refused.
    """
    now = timezone.now()
    with transaction.atomic():
        counter, created = RateLimitCounter.objects.select_for_update().get_or_create(
            key=key,
            defaults={"count": 1, "expires_at": now + timedelta(seconds=window_seconds * 2)},
        )
        if created:
            # A new key means a new window, which is a cheap, naturally rate-limited
            # moment to drop the rows the previous ones left behind. Housekeeping
            # proper arrives with issue #5.
            _prune(now)
            return limit < 1

        counter.count += 1
        counter.save(update_fields=["count", "updated_at"])
        return counter.count > limit


def _prune(now: datetime) -> None:
    deleted, _ = RateLimitCounter.objects.filter(expires_at__lt=now).delete()
    if deleted:
        logger.debug("Pruned %s expired rate-limit counters", deleted)
