"""``route_events`` — the processor that replaces contract 6's no-op tail.

    ``register_processors()`` registers persistence, then registers ``route_events``
    — a documented **no-op** — under ``ROUTING_PROCESSOR``. Its docstring says
    what #11 does: register your own callable under the same name.

So this module's public shape is one function with the seam's signature, and the
five stages layer *inside* it rather than changing that signature.

Three properties, in the order they break things when wrong.

**The contact advisory lock is taken here, in this transaction.**
``persist_events`` deliberately takes none — it appends rows the database
arbitrates through unique constraints, and blocking there would spend SPEC §7.1's
budget waiting on a worker — and a transaction-scoped lock cannot span two
processors anyway. Inline it is ``try_contact_lock``: SPEC §9.6 says the web
request enqueues rather than blocks. In the worker it is the blocking one, which
is what a worker is for.

**Nesting is free, not lucky.** ``attempt_resume`` and ``start_flow`` each open
their own ``transaction.atomic()`` and take ``contact_lock`` again. Postgres
advisory locks are counted per session and both are ``pg_advisory_xact_lock`` on
the same session over the same key (``contact_lock_key`` normalises through
``coerce_contact_id``, so an object and an id hash identically), so the second
acquisition returns immediately and both release at COMMIT. On the worker path
there are three acquisitions — ``process_action``'s, ours, the engine's — and the
same argument covers all three.

**A 200 is owed to the platform whatever happens.** SPEC §7.1: "Never return 5xx
for business-logic failures". One ``try`` per event, so an unparseable event does
not cost the five good ones delivered beside it.
"""

import logging
import time
from collections.abc import Sequence
from typing import Any

from django.db import transaction

from apps.channels.events import EventType, NormalizedEvent
from apps.flows.triggers import handlers
from apps.flows.triggers.budget import (
    COURTESY_BUDGET_SECONDS,
    InlineBudget,
    InlineDecision,
    may_run_inline,
    note_inline_latency,
)
from apps.flows.triggers.context import RoutingContext, RoutingMode, build_context
from apps.flows.triggers.hooks import (
    ENGINE_STAGES,
    RUNS_WHILE_PAUSED,
    Consumed,
    Deferred,
    HookAbortedError,
    Stage,
    run_stage,
    stages_from,
)
from apps.flows.triggers.safety import first_step_is_safe

__all__ = ["ROUTABLE_EVENTS", "ROUTING_PROCESSOR", "register_routing", "route_events", "route_one"]

logger = logging.getLogger(__name__)

#: Must equal ``apps.messaging.ingest.ROUTING_PROCESSOR``.
#:
#: Duplicated as a literal rather than imported so ``apps.flows`` still never
#: imports ``apps.messaging`` at module scope — the property ``apps/flows/messaging.py``
#: exists to preserve. ``test_routing_seam.py`` pins the two constants equal, so
#: the duplication cannot drift silently.
ROUTING_PROCESSOR = "routing"

#: A delivery receipt routes nowhere: persistence already walked the message
#: status ladder with it, and there is no contact-authored content to react to.
ROUTABLE_EVENTS = frozenset(EventType) - {EventType.DELIVERY_STATUS}


def register_routing() -> None:
    """Take over contract 6's routing slot. Called from ``FlowsConfig.ready()``.

    Unguarded on purpose, and it is the mirror image of the guard in
    ``apps.messaging.ingest.register_processors``. That one refuses to install
    its no-op when a real router already holds the name; this one always claims
    the name, because ``ready()`` runs in ``INSTALLED_APPS`` order and
    ``apps.messaging`` is listed first. Registering under an existing name
    replaces **in place**, so the real router inherits the slot *after*
    persistence and nothing in ``apps.messaging`` or ``apps.channels`` changes.
    """
    from django.apps import apps as django_apps

    if not django_apps.is_installed("apps.channels"):  # pragma: no cover - always installed
        return
    from apps.channels import ingest as channels_ingest

    channels_ingest.register_processor(route_events, name=ROUTING_PROCESSOR)


def route_events(connection: Any, events: Sequence[NormalizedEvent]) -> None:
    """Contract 6's routing tail, for one connection's share of a delivery."""
    budget = InlineBudget.start()
    for event in events:
        if getattr(event, "type", None) not in ROUTABLE_EVENTS:
            continue
        try:
            context = build_context(connection, event, budget, mode=RoutingMode.INLINE)
            if context is not None:
                route_one(context)
        except Exception:
            # Broad and logged rather than raised, for the same reason
            # persist_events is: the seam turns a raising processor into a
            # failed *batch*. Nothing platform-supplied reaches the log line —
            # an attacker-controlled id in a log message is a log-injection
            # primitive. The event type is ours and the connection id is a UUID.
            logger.exception(
                "Routing failed for a %s event on connection %s",
                getattr(event, "type", "?"),
                connection.pk,
            )


def route_one(context: RoutingContext, *, from_stage: Stage = Stage.HARD_OPTOUT) -> None:
    """Walk the stages for one event, inline or handing off as the budget allows."""
    try:
        _route(context, from_stage)
    except HookAbortedError as exc:
        # Fail closed: a hard_optout hook raised, so we do not know whether this
        # contact has unsubscribed, and nothing downstream may send to them.
        logger.warning("Abandoning a %s event: opt-out hook %s failed.", context.event.type, exc)


def _route(context: RoutingContext, from_stage: Stage) -> None:
    stages = stages_from(from_stage)

    # 1. The lock-free stages, outside any transaction of ours. Neither advances
    #    a state machine, and holding a lock across L6-C's inbox rules would put
    #    a rule engine inside the one-step-per-contact invariant for no reason.
    for stage in stages:
        if stage in ENGINE_STAGES:
            break
        if context.is_paused and stage not in RUNS_WHILE_PAUSED:
            continue
        context.stage = stage
        outcome = run_stage(stage, context)
        if isinstance(outcome, Consumed):
            return
        if isinstance(outcome, Deferred):
            handlers.hand_off(context, stage, reason=outcome.reason)
            return

    engine_stages = tuple(stage for stage in stages if stage in ENGINE_STAGES)
    if not engine_stages:
        return

    # 2. SPEC §14: an agent has taken over, so every remaining stage is
    #    automation and none of it runs. Nothing is written to record the skip,
    #    which is exactly why eligibility returns by itself when the pause
    #    lapses — the execution is still waiting and no retry counter moved.
    if context.is_paused:
        logger.debug("Automation is paused for this contact; skipping %s.", engine_stages)
        return

    if not context.can_run_engine:
        # A comment: no contact, so nothing to lock and no flow to start. The
        # trigger stage still runs, because matching one is what claims the
        # comment and tells L5-A's private reply there is work to do.
        #
        # Its outcome is honoured rather than discarded: a hook here is under the
        # same contract as anywhere else, so an L5 comment hook that cannot
        # finish inline must be able to say ``Deferred`` and have the event
        # reach the worker instead of ending here.
        context.stage = Stage.TRIGGER
        outcome = run_stage(Stage.TRIGGER, context)
        if isinstance(outcome, Deferred):
            handlers.hand_off(context, Stage.TRIGGER, reason=outcome.reason)
        return

    if context.mode is RoutingMode.WORKER:
        _run_engine_stages(context, engine_stages, blocking=True)
        return

    decision = _inline_decision(context, engine_stages[0])
    if not decision.is_inline:
        handlers.hand_off(context, engine_stages[0], reason=decision.value)
        return

    started = time.monotonic()
    try:
        _run_engine_stages(context, engine_stages, blocking=False)
    finally:
        note_inline_latency(context.connection, time.monotonic() - started)


def _run_engine_stages(context: RoutingContext, stages: tuple[Stage, ...], *, blocking: bool) -> None:
    """Resume, trigger and default reply — one transaction, one lock."""
    from apps.queueing.locks import contact_lock, try_contact_lock

    with transaction.atomic():
        if blocking:
            with contact_lock(context.contact):
                _walk(context, stages)
            return
        with try_contact_lock(context.contact) as acquired:
            if not acquired:
                # SPEC §9.6: enqueue rather than block the web request. A
                # gunicorn thread waiting behind whatever the worker is doing to
                # this contact is the whole failure this branch exists to avoid.
                handlers.hand_off(context, stages[0], reason="lock_contention")
                return
            _walk(context, stages)


def _walk(context: RoutingContext, stages: tuple[Stage, ...]) -> None:
    for index, stage in enumerate(stages):
        if index and context.budget.exhausted():
            # Between stages, not mid-flow: the runner has no interrupt, and
            # anything already inside it is durable at every pause. What is left
            # of the chain becomes a queue row rather than a lost event.
            handlers.hand_off(context, stage, reason="budget")
            return
        context.stage = stage
        outcome = run_stage(stage, context)
        if isinstance(outcome, Consumed):
            return
        if isinstance(outcome, Deferred):
            handlers.hand_off(context, stage, reason=outcome.reason)
            return


def _inline_decision(context: RoutingContext, stage: Stage) -> InlineDecision:
    """SPEC §7.1 step 4, including the courtesy calls it puts first."""
    decision = may_run_inline(
        context.connection,
        context.budget,
        first_step_safe=first_step_is_safe(context, stage),
    )
    if not decision.is_inline:
        return decision

    _courtesy_calls(context)

    # Re-checked *after* the courtesy calls, which is the point of making them
    # first: they hit the same API the send will, so a connection that took most
    # of the budget to acknowledge a typing indicator has already told us it will
    # not finish a reply inside what is left.
    if context.budget.exhausted():
        note_inline_latency(context.connection, float("inf"))
        return InlineDecision.BUDGET
    return decision


def _courtesy_calls(context: RoutingContext) -> None:
    """``mark_seen`` then ``send_typing``, where the platform has them.

    The first caller either has ever had in this project. Best-effort in the
    strongest sense: each is wrapped on its own, because an adapter blowing up
    while being polite must not cost the contact their reply.
    """
    from apps.channels.capabilities import capabilities_for
    from apps.channels.registry import adapter_for

    if context.identity is None or not context.budget.allows(COURTESY_BUDGET_SECONDS):
        return
    try:
        capabilities = capabilities_for(context.connection.platform)
    except KeyError:  # pragma: no cover - every platform is in the table
        return
    if not capabilities.typing_indicator:
        return
    try:
        adapter = adapter_for(context.connection.platform)
    except LookupError:
        # No adapter registered for this platform yet. Nothing to be polite
        # with, and the send that follows will report it properly.
        return

    for call in ("mark_seen", "send_typing"):
        try:
            getattr(adapter, call)(context.connection, context.identity)
        except Exception:
            logger.warning("%s failed on connection %s; continuing.", call, context.connection.pk, exc_info=True)
