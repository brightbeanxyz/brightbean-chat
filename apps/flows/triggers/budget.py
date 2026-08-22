"""SPEC §7.1 step 4: run it now, or hand it to the worker.

    If a waiting execution or trigger matches AND the resulting first step is
    synchronous-safe […] execute inline under a total budget of 1.5 s wall clock
    including the outbound API call (2 s hard timeout on the HTTP client). Fire
    ``mark_seen`` + ``send_typing`` first where supported. […] Budget exceeded,
    node is not synchronous-safe, or any error: enqueue.

Four gates, all of which must pass: the budget is not spent, the connection is
not known to be slow, the first step is synchronous-safe, and the connection has
a send token. Anything else enqueues, which is always correct and never lossy.

Two of them are worth explaining.

**The capacity check must not spend a token.** ``messaging.services.send_outbound``
already performs the non-blocking acquire SPEC §8 asks for and defers the send
when the bucket is empty, so a debiting acquire here would charge every inline
single-send flow twice — halving effective throughput and manufacturing
``send_retry`` rows. ``try_acquire(cost=0.0)`` refills the bucket and debits
nothing while still reporting what is in it, so the one real debit stays where
the send is.

**The slow-connection circuit is what makes the ack budget reachable.** Routing
cannot predict a slow ``send``; it can decline to start one. ``mark_seen`` and
``send_typing`` are fired first — SPEC says so — which makes them a free probe of
how fast this platform is answering *right now*. One overrun flags the
connection, and every event on it enqueues before doing any I/O at all until the
flag expires.
"""

import logging
import time
from dataclasses import dataclass
from enum import StrEnum
from math import inf
from typing import Any

from django.conf import settings
from django.core.cache import cache

__all__ = [
    "COURTESY_BUDGET_SECONDS",
    "INLINE_BUDGET_SECONDS",
    "SLOW_CONNECTION_TTL_SECONDS",
    "InlineBudget",
    "InlineDecision",
    "clear_slow_connection",
    "connection_is_slow",
    "has_send_capacity",
    "inline_routing_enabled",
    "may_run_inline",
    "note_inline_latency",
]

logger = logging.getLogger(__name__)


def _setting(name: str, default: float) -> float:
    """A tunable that needs no ``config/settings`` edit to exist.

    Every value here has a SPEC-given default and no deployment should need to
    change one; a setting that is only read through ``getattr`` stays available
    to the operator who does, without adding a name to a settings module four
    parallel workstreams are also editing.
    """
    return float(getattr(settings, name, default))


#: SPEC §7.1's wall-clock budget for an inline reply. The matching "2 s hard
#: timeout on the HTTP client" is already ``providers/base.READ_TIMEOUT``; there
#: is deliberately no second copy of it here.
INLINE_BUDGET_SECONDS = 1.5

#: How much budget the ``mark_seen``/``send_typing`` probe may spend before the
#: connection counts as too slow to reply to inline.
COURTESY_BUDGET_SECONDS = 0.5

#: How long a connection stays flagged slow. Short: a platform's bad minute
#: should not cost it the rest of the hour.
SLOW_CONNECTION_TTL_SECONDS = 60

_SLOW_KEY = "flows:inline-slow:{pk}"


class InlineDecision(StrEnum):
    """Why routing chose the path it chose. Logged, and asserted on in tests."""

    INLINE = "inline"
    DISABLED = "disabled"
    BUDGET = "budget"
    SLOW_CONNECTION = "slow_connection"
    NOT_SYNCHRONOUS_SAFE = "not_synchronous_safe"
    NO_SEND_CAPACITY = "no_send_capacity"

    @property
    def is_inline(self) -> bool:
        return self is InlineDecision.INLINE


@dataclass
class InlineBudget:
    """A wall-clock deadline, or none at all on the worker.

    One per webhook *batch*, not per event: a Meta delivery carrying ten events
    must not be allowed ten times 1.5 seconds.
    """

    deadline: float | None

    @classmethod
    def start(cls, seconds: float | None = None) -> "InlineBudget":
        budget = _setting("INLINE_ROUTING_BUDGET_SECONDS", INLINE_BUDGET_SECONDS) if seconds is None else seconds
        return cls(deadline=time.monotonic() + budget)

    @classmethod
    def unbounded(cls) -> "InlineBudget":
        """The worker's budget. There is no client on the other end of a socket."""
        return cls(deadline=None)

    def remaining(self) -> float:
        if self.deadline is None:
            return inf
        return self.deadline - time.monotonic()

    def exhausted(self) -> bool:
        return self.remaining() <= 0

    def allows(self, need: float) -> bool:
        """Whether there is room for something expected to take ``need`` seconds."""
        return self.remaining() >= need


def inline_routing_enabled() -> bool:
    """The operator's escape hatch: run everything through the worker."""
    return bool(getattr(settings, "INLINE_ROUTING_ENABLED", True))


def connection_is_slow(connection: Any) -> bool:
    """Whether this connection recently overran the inline budget."""
    return bool(cache.get(_SLOW_KEY.format(pk=connection.pk)))


def note_inline_latency(connection: Any, elapsed: float) -> None:
    """Record how long an inline attempt took, and flag the connection if it overran."""
    if elapsed < _setting("INLINE_ROUTING_BUDGET_SECONDS", INLINE_BUDGET_SECONDS):
        return
    ttl = int(_setting("INLINE_ROUTING_SLOW_TTL_SECONDS", SLOW_CONNECTION_TTL_SECONDS))
    cache.set(_SLOW_KEY.format(pk=connection.pk), True, ttl)
    logger.info(
        "Connection %s took %.2fs inline; routing its events through the worker for %ss.",
        connection.pk,
        elapsed,
        ttl,
    )


def clear_slow_connection(connection: Any) -> None:
    """Forget the flag. For an operator, and for tests that need a clean slate."""
    cache.delete(_SLOW_KEY.format(pk=connection.pk))


def has_send_capacity(connection: Any, *, cost: float = 1.0) -> bool:
    """Whether the connection's token bucket could pay for a send — without paying.

    See the module docstring: ``cost=0.0`` refills and reports, and the single
    real debit stays in the send pipeline where the send is.
    """
    from apps.flows import messaging as messaging_facade

    tokens = messaging_facade.send_bucket_tokens(connection)
    if tokens is None:  # pragma: no cover - messaging is installed everywhere
        return True
    return tokens >= cost


def may_run_inline(connection: Any, budget: InlineBudget, *, first_step_safe: bool) -> InlineDecision:
    """The whole of SPEC §7.1 step 4, in the order that costs least to answer."""
    if not inline_routing_enabled():
        return InlineDecision.DISABLED
    if budget.exhausted():
        return InlineDecision.BUDGET
    if connection_is_slow(connection):
        return InlineDecision.SLOW_CONNECTION
    if not first_step_safe:
        return InlineDecision.NOT_SYNCHRONOUS_SAFE
    if not has_send_capacity(connection):
        return InlineDecision.NO_SEND_CAPACITY
    return InlineDecision.INLINE
