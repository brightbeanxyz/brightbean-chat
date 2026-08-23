"""The inbound dispatch seam — ROADMAP contract 6, first half.

    L2-B's webhook endpoint does verify → dedup → raw-persist →
    ``dispatch(NormalizedEvent)`` against a pluggable processor registration
    point (default no-op). L3-A registers persistence; L4-A replaces the tail
    with an **ordered hook registry with named stages**.

So this module is a registration point and a loop, and **nothing else ships
here**. There is no persistence, no trigger matching, no compliance — every one
of those belongs to a later issue, and hard-coding any of them now is exactly
what the seam exists to prevent.

Processors are ordered and named. Named because a test (and, later, L4-A's
replacement of the tail) has to be able to take one out again, and because
re-registering under the same name replaces rather than stacks — a module
imported twice must not double-process every event. Ordered because L3-A's
persistence has to run before L4-A's routing can look at what it wrote.

**A processor that raises does not fail the request.** SPEC §7.1 is explicit:
"Never return 5xx for business-logic failures". The exception is logged, the
event is marked failed in the log, and the platform gets its 200 — because a 5xx
makes the platform retry a delivery that will fail identically, and enough of
those get a webhook disabled at the provider's end.
"""

import hashlib
import itertools
import json
import logging
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from apps.channels.events import NormalizedEvent
    from apps.channels.models import ChannelConnection

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_ORDER",
    "LATE_ORDER",
    "Processor",
    "process_events",
    "register_processor",
    "registered_processors",
    "synthetic_event_id",
    "unregister_processor",
]

#: What a processor looks like. Return value ignored: the seam is a fan-out, not
#: a pipeline that transforms events. L4-A's named stages layer *inside* one
#: processor rather than changing this signature.
type Processor = Callable[["ChannelConnection", Sequence["NormalizedEvent"]], None]

#: The order band an ordinary stage registers in. Within one band, dispatch
#: follows registration order, which is what keeps persistence ahead of the
#: routing tail it feeds without either of them naming a number.
DEFAULT_ORDER = 0

#: For a stage that must run after everything else, whoever else registers and
#: whatever order the apps happen to load in. SPEC §16's flow preview uses it:
#: it needs the contact persistence created **and** it supersedes whatever the
#: routing tail started, so "last" is a property of the stage rather than of
#: where its app sits in ``INSTALLED_APPS``.
LATE_ORDER = 100

_PROCESSORS: dict[str, Processor] = {}

#: name -> (order band, registration sequence). Dispatch sorts on this pair, so
#: a stage's position is something it *declares* rather than something it
#: inherits from ``INSTALLED_APPS``. That distinction is load-bearing: app
#: readiness runs in ``INSTALLED_APPS`` order, ``apps.channels`` is listed
#: before ``apps.messaging``, and a stage that must follow persistence therefore
#: could not express that by registering from the app that owns its code.
_ORDER: dict[str, tuple[int, int]] = {}

_SEQUENCE = itertools.count()


def register_processor(processor: Processor, *, name: str, order: int = DEFAULT_ORDER) -> None:
    """Register ``processor`` under ``name``, replacing any previous holder.

    Replacement rather than refusal, unlike the adapter registry: a processor
    name identifies a *stage* ("persistence", "routing"), and a later layer
    taking over a stage its predecessor stubbed is the intended lifecycle. A
    replacement keeps the original stage's **position** — L4-A taking over the
    routing tail must inherit the slot after persistence, not move to the end.

    ``order`` is the band the stage runs in; ties break on registration order.
    Leave it at :data:`DEFAULT_ORDER` unless the stage genuinely has to run
    after stages it does not know the names of.
    """
    if not name:
        raise ValueError("A processor needs a name so it can be replaced or removed later.")
    if name in _PROCESSORS:
        logger.debug("Replacing inbound processor %r", name)
        _ORDER[name] = (order, _ORDER[name][1])
    else:
        _ORDER[name] = (order, next(_SEQUENCE))
    _PROCESSORS[name] = processor


def unregister_processor(name: str) -> None:
    """Remove a processor. Unknown names are ignored."""
    _PROCESSORS.pop(name, None)
    _ORDER.pop(name, None)


def registered_processors() -> tuple[str, ...]:
    """Registered processor names, in dispatch order."""
    return tuple(sorted(_PROCESSORS, key=lambda name: _ORDER[name]))


def process_events(connection: "ChannelConnection", events: Sequence["NormalizedEvent"]) -> bool:
    """Hand ``events`` to every registered processor, in order.

    Returns True when every processor completed. With none registered — which is
    the state this issue ships in — that is trivially true and nothing happens,
    which is the correct behaviour for a framework whose consumers have not
    merged yet.

    Each processor is isolated in two senses: one that raises does not stop the
    next, because "persistence failed" should not also mean "the STOP keyword
    was ignored"; and each receives an immutable snapshot of the batch, so no
    stage can rewrite the input of the stages after it.
    """
    if not events:
        return True
    if not _PROCESSORS:
        logger.debug(
            "No inbound processors registered; %s event(s) on connection %s were logged and dropped. "
            "L3-A (#8) registers persistence and L4-A (#11) the routing tail.",
            len(events),
            connection.pk,
        )
        return True

    # A tuple, built once and handed to every processor. They previously all
    # received the same list object, so one stage appending, filtering or
    # reordering in place changed what every later stage saw — and the result
    # depended on registration order, which is exactly the coupling a seam is
    # supposed to remove.
    #
    # This makes the *sequence* immutable. NormalizedEvent is frozen, but
    # `raw` and `EventPayload.extra` are ordinary dicts and stay mutable; the
    # contract, stated here and on NormalizedEvent, is that processors treat a
    # dispatched event as read-only. Deep-copying per processor would enforce it
    # outright, and is deliberately not paid for on a path SPEC §7.1 budgets at
    # 1.5 s of wall clock including the outbound call.
    snapshot = tuple(events)

    ok = True
    for name in registered_processors():
        processor = _PROCESSORS[name]
        try:
            processor(connection, snapshot)
        except Exception:
            # Broad on purpose: a processor is third-party-ish code from the
            # endpoint's point of view, and the endpoint's contract with the
            # platform is a 200. logger.exception scrubs credentials out of the
            # traceback (apps.common.logging).
            logger.exception("Inbound processor %r failed for connection %s", name, connection.pk)
            ok = False
    return ok


def synthetic_event_id(payload: Any, *, prefix: str = "") -> str:
    """A deterministic event id for a platform that does not supply one.

    Dedup (SPEC §7.1 step 2) is keyed on ``provider_event_id``. A platform that
    sends no stable id per event — Twilio's form posts, some Meta status
    batches — would otherwise get every retry processed again, so the id is
    derived from the event's own content instead.

    Deterministic hashing of the *content* is what makes a retry of the same
    event collide, which is the entire point. Two genuinely identical events
    (the same contact sending "hi" twice within the retention window) would also
    collide; adapters avoid that by including the platform's timestamp in the
    payload they hash, which every platform does send.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:40]
    return f"{prefix}{digest}" if prefix else digest
