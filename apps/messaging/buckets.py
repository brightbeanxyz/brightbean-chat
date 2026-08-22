"""Postgres token buckets, one per channel connection (SPEC §8).

SPEC §22 is absolute: "No Redis ever in v1; Postgres is the queue, the lock
manager, and the rate limiter." So the bucket is a row, and the read-and-mutate
happens inside one transaction holding ``select_for_update`` — the house pattern
``apps.common.ratelimit`` documents, and for the reason its docstring gives:
``DatabaseCache.incr`` is a get-then-set that loses counts under exactly the
concurrency a rate limiter exists to survive.

--------------------------------------------------------------------------
Why the clock is Postgres's
--------------------------------------------------------------------------

Elapsed time is measured with ``clock_timestamp()``, fetched in the same round
trip as the row lock. Not ``timezone.now()``: two workers on two hosts have two
clocks, and one running five seconds fast writes a ``refilled_at`` in the future
that freezes the bucket for everybody until real time catches up. Not Django's
``Now()`` either, which is ``CURRENT_TIMESTAMP`` — *transaction start* — so a
transaction that waited on the row lock would under-count the time it waited and
silently deliver below the configured rate.

--------------------------------------------------------------------------
Blocking, and the bound on it
--------------------------------------------------------------------------

SPEC §8 wants the worker to respect buckets and "the inline path performs a
non-blocking acquire and falls back to enqueue when empty", so both exist here.
The bound on the blocking one is the honest part, and it is worth stating
plainly because it is the one place this module trades a property away.

``apps.queueing.worker.process_action`` wraps every handler in
``transaction.atomic()``, and a ``select_for_update`` row lock is released at
the *enclosing* transaction's commit. So a token acquired inside a worker
handler keeps the bucket row locked until that handler returns — across the
provider call. Two workers sending on one connection therefore serialise behind
each other rather than interleaving at the configured rate. That is always
*safe*: it can only send below the rate, never above, so the throttle is never
violated. It is not free: on a slow platform the effective throughput becomes
one send per round trip rather than ``rate`` per second.

An unbounded blocking acquire would make that much worse — a worker sleeping
inside its handler holds an open transaction, a contact advisory lock and one of
N worker slots, so one throttled connection could stall unrelated queue work.
Hence :data:`SEND_BUCKET_MAX_WAIT_SECONDS`: past it, :func:`acquire` returns
:class:`Deferred` and the caller turns the wait into a scheduled ``send_retry``,
which is the queue doing the waiting instead of a worker. Setting it to ``0``
degrades ``acquire`` to ``try_acquire`` for an operator who wants that.

If the serialisation ever bites, the fix is a second database connection for
this module alone, so the bucket transaction commits before the provider call.
That is a real change to how the app talks to Postgres and is deliberately not
being made for a throughput problem nobody has measured yet.
"""

import logging
import time
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.db import models, transaction
from django.db.models.functions import Now

from apps.channels.policy import policy_for
from apps.messaging.models import SendBucket

logger = logging.getLogger(__name__)

__all__ = [
    "Acquisition",
    "Deferred",
    "Granted",
    "acquire",
    "capacity_for",
    "rate_for",
    "try_acquire",
]

#: Floor on a configured rate. A zero or negative rate would mean "never send",
#: which is a misconfiguration rather than a policy, and the check constraint on
#: the row refuses it anyway.
MIN_RATE = 0.01

#: How long :func:`acquire` will wait before giving up and letting the queue do
#: the waiting. Same order as the provider call it is about to make.
DEFAULT_MAX_WAIT_SECONDS = 2.0

#: How often the blocking path re-checks. Short enough that several waiters stay
#: roughly fair rather than one sleeping through the others' turns.
POLL_SECONDS = 0.025


class ClockTimestamp(models.Func):
    """Postgres ``clock_timestamp()`` — the real clock, not transaction start."""

    function = "clock_timestamp"
    arity = 0
    output_field = models.DateTimeField()


@dataclass(frozen=True)
class Granted:
    """A token was taken. The send may proceed."""

    tokens_left: float


@dataclass(frozen=True)
class Deferred:
    """No token. ``wait_seconds`` is when one is next due."""

    wait_seconds: float


type Acquisition = Granted | Deferred


def rate_for(platform: str) -> float:
    """Sends per second for ``platform``.

    ``settings.DEFAULT_SEND_RATE_OVERRIDES`` (SPEC §20's env var) wins over
    ``PlatformPolicy.rate_default``; nothing else participates. Keyed by
    platform rather than by connection because the setting is named ``DEFAULT_``
    and per-connection tuning belongs on the connection row, not in a
    deployment-wide environment variable.
    """
    override = settings.DEFAULT_SEND_RATE_OVERRIDES.get(platform)
    rate = float(override) if override is not None else policy_for(platform).rate_default
    return max(rate, MIN_RATE)


def capacity_for(rate: float) -> float:
    """How much burst a bucket holds — one second's worth, at least one token.

    At least one, or a platform configured below 1/s could never accumulate
    enough to send at all.
    """
    return max(1.0, rate * settings.SEND_BUCKET_BURST_SECONDS)


def try_acquire(connection: Any, *, cost: float = 1.0) -> Acquisition:
    """Take one token if there is one. Never sleeps.

    SPEC §7.1's inline path: "performs a non-blocking acquire and falls back to
    enqueue when empty".

    One query in the steady state. An earlier version ran ``get_or_create``
    first and then the locked read, so every send cost two round trips to find
    its bucket — and :func:`acquire`'s poll loop multiplied that by however many
    times it woke up. The row is created only when the locked read finds nothing,
    which is once in a connection's lifetime.
    """
    rate = rate_for(connection.platform)
    capacity = capacity_for(rate)

    with transaction.atomic():
        bucket = (
            SendBucket.objects.select_for_update()
            .annotate(db_now=ClockTimestamp())
            .filter(connection=connection)
            .first()
        )
        if bucket is None:
            return _create_full(connection, rate, capacity, cost)

        now = bucket.db_now
        # Re-read the configured rate on every acquire, so a changed
        # DEFAULT_SEND_RATE_OVERRIDES takes effect at the next send rather than
        # needing a migration, a management command or a stale row to be noticed.
        bucket.refill_rate = rate
        bucket.capacity = capacity

        elapsed = max(0.0, (now - bucket.refilled_at).total_seconds())
        available = min(capacity, bucket.tokens + elapsed * rate)
        granted = available >= cost

        bucket.tokens = available - cost if granted else available
        bucket.refilled_at = now
        bucket.save(update_fields=["tokens", "capacity", "refill_rate", "refilled_at", "updated_at"])

        if not granted:
            return Deferred(wait_seconds=(cost - available) / rate)
        return Granted(tokens_left=bucket.tokens)


def acquire(connection: Any, *, cost: float = 1.0, max_wait: float | None = None) -> Acquisition:
    """Take one token, waiting up to ``max_wait`` seconds for it.

    Returns :class:`Deferred` rather than waiting longer, so the caller can
    schedule instead. Read the module docstring before raising the bound: the
    wait happens with the caller's transaction open.
    """
    budget = DEFAULT_MAX_WAIT_SECONDS if max_wait is None else max_wait
    if budget <= 0:
        return try_acquire(connection, cost=cost)

    deadline = time.monotonic() + budget
    while True:
        outcome = try_acquire(connection, cost=cost)
        if isinstance(outcome, Granted):
            return outcome
        remaining = deadline - time.monotonic()
        if outcome.wait_seconds > remaining:
            return outcome
        time.sleep(min(outcome.wait_seconds, POLL_SECONDS, max(remaining, 0.0)))


def _create_full(connection: Any, rate: float, capacity: float, cost: float) -> Acquisition:
    """First use of a connection: create the bucket full and spend from it.

    Full rather than empty so a connection's first message does not wait a
    second for a bucket nobody has drained. The burst it permits is one
    ``capacity``, which is the burst the configuration already asks for.

    ``get_or_create`` rather than a plain create, because two processes can send
    a connection's first two messages at once and the one-to-one is unique; the
    loser re-reads instead of raising. Its row is authoritative and this call
    simply grants against the fresh capacity, which is at most one extra token
    at the very start of a connection's life.
    """
    SendBucket.objects.get_or_create(
        connection=connection,
        defaults={
            "tokens": max(capacity - cost, 0.0),
            "capacity": capacity,
            "refill_rate": rate,
            # Now(), not timezone.now(): the row's clock and the refill
            # arithmetic's clock have to be the same one.
            "refilled_at": Now(),
        },
    )
    return Granted(tokens_left=max(capacity - cost, 0.0))
