"""The hooks this issue registers into contract 6's stages.

Four of the five stages get one hook each; ``post_persist`` ships **empty and
named**, which is the point of it — L6-C's inbox rules go there, and the stage
existing and being tested now is what lets that be a registration rather than an
edit to routing code.

Read this file before writing a hook of your own: it is the working example the
layer prompt points L5-D and L6-C at.

One thing L5-D specifically needs to know. A ``hard_optout`` hook **cannot write
``identity.opted_out_at``** — contract 3 gives that column one write site,
``apps/messaging/ingest.py``, and ``apps/messaging/tests/test_write_sites.py``
fails the build over it. Ingest already applies it from an ``EventType.OPT_OUT``
event, so an SMS adapter's job is to classify ``STOP``/``UNSUBSCRIBE`` as that
event type in ``parse_events``; the hook then owns the confirmation reply and
consuming the event, which is what :func:`opt_out_event` below does for the
platform-agnostic half.
"""

import logging
from typing import Any

from apps.channels.events import EventType
from apps.flows.engine import FlowNotRunnableError, start_flow
from apps.flows.engine.waits import Consumed as ResumeConsumed
from apps.flows.engine.waits import attempt_resume
from apps.flows.models import ExecutionStatus, FlowExecution, FlowStatus, StartedBy, Trigger, TriggerType
from apps.flows.triggers.context import RoutingContext
from apps.flows.triggers.guards import claim_default_reply
from apps.flows.triggers.hooks import Consumed, HookOutcome, Passed, Stage, register_hook
from apps.flows.triggers.matching import MatchContext, match

__all__ = [
    "DEFAULT_REPLY_EVENTS",
    "REPLY_EVENTS",
    "default_reply",
    "default_reply_trigger_for",
    "opt_out_event",
    "register_builtin_hooks",
    "trigger_match",
    "waiting_execution",
]

logger = logging.getLogger(__name__)

#: Event types that could plausibly be a *reply* and may therefore be offered to
#: a waiting execution.
#:
#: The exclusions matter more than the inclusions. A ``referral`` handed to
#: ``attempt_resume`` while an execution waits on buttons matches neither a
#: button id nor a valid answer, falls into the retry path, and — if a retry
#: remains — is **consumed by a retry prompt**, swallowing the ref link the
#: contact just clicked and burning a retry they never used. ``attempt_resume``'s
#: own docstring says precedence is L4-A's to decide; this is that decision.
REPLY_EVENTS = frozenset({EventType.MESSAGE, EventType.POSTBACK, EventType.STORY_REPLY})

#: What a default reply is an answer to. A postback is a button press, which is
#: never "I didn't understand you"; a follow or a referral is not a message.
DEFAULT_REPLY_EVENTS = frozenset({EventType.MESSAGE, EventType.STORY_REPLY})


def opt_out_event(context: RoutingContext) -> HookOutcome:
    """Consume a normalised opt-out so nothing else acts on it.

    Small, and load-bearing. Persistence has already set ``opted_out_at`` from
    this event; without a hook here the same event would fall through to trigger
    matching, where a keyword trigger on the word "STOP" would cheerfully start a
    flow at somebody who just unsubscribed.

    It deliberately does **not** consume every event from an already-opted-out
    identity. That would make L5-D's re-subscribe keyword unreachable, and SPEC
    §19 puts opt-out enforcement in the compliance engine precisely so it is one
    chokepoint rather than a rule each layer reimplements.
    """
    if context.event.type == EventType.OPT_OUT:
        return Consumed("opt-out")
    return Passed()


def waiting_execution(context: RoutingContext) -> HookOutcome:
    """SPEC §9.3 step 2: offer the event to the execution waiting on this channel.

    ``Consumed`` covers both "the reply matched and the flow moved on" and "the
    reply did not match but was answered with a retry prompt" — §9.3 groups them,
    because in both cases the contact has had a response and the event must not
    also fire a keyword trigger. ``NotConsumed`` falls through, which is how
    "keywords still work mid-flow only if nothing consumed the event" is true.
    """
    if context.event.type not in REPLY_EVENTS or context.contact is None:
        return Passed("not a reply")

    execution = _waiting_execution_for(context)
    if execution is None:
        return Passed("nothing waiting")

    outcome = attempt_resume(execution, context.event)
    if isinstance(outcome, ResumeConsumed):
        return Consumed(outcome.reason or "resumed")
    return Passed(outcome.reason or "not for this execution")


def trigger_match(context: RoutingContext) -> HookOutcome:
    """SPEC §9.3 step 3: the first trigger that matches, in priority order."""
    found = match(MatchContext.from_event(context.connection, context.event, contact=context.contact))
    if found is None:
        return Passed("no trigger matched")

    if context.contact is None:
        # A comment. The trigger matched, but there is nobody to run a flow for
        # until a private reply opens a DM thread — L5-A and L5-B own that half.
        context.notes["matched_trigger_id"] = str(found.trigger.pk)
        return Consumed(f"{found.trigger.type} matched, awaiting a contact")

    if not _start(context, found.trigger, found.variables):
        return Passed("the matched flow could not run")
    return Consumed(f"{found.trigger.type} trigger")


def default_reply(context: RoutingContext) -> HookOutcome:
    """SPEC §9.3 step 4: nothing matched, so answer once per contact per day."""
    if context.event.type not in DEFAULT_REPLY_EVENTS or context.contact is None:
        return Passed("not a message")

    trigger = default_reply_trigger_for(context)
    if trigger is None:
        return Passed("no default reply configured")

    # Claimed before the flow starts and inside the caller's transaction, so a
    # failure to start rolls the claim back with it rather than silently costing
    # this contact their one reply for the day.
    if not claim_default_reply(context.contact, context.connection):
        return Passed("already answered within 24h")

    if not _start(context, trigger, {"trigger_type": trigger.type}):
        return Passed("the default-reply flow could not run")
    return Consumed("default reply")


def register_builtin_hooks() -> None:
    """Called from ``FlowsConfig.ready()``. Idempotent."""
    for hook, stage, name in (
        (opt_out_event, Stage.HARD_OPTOUT, "opt_out_event"),
        (waiting_execution, Stage.RESUME, "waiting_execution"),
        (trigger_match, Stage.TRIGGER, "trigger_match"),
        (default_reply, Stage.DEFAULT_REPLY, "default_reply"),
    ):
        register_hook(hook, stage=stage, name=name, replace_existing=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _waiting_execution_for(context: RoutingContext) -> FlowExecution | None:
    """The execution waiting for a reply from this contact, on this channel.

    SPEC §22 keeps at most one live execution per contact, so this is a
    ``.first()`` over what should be a single row rather than a ``.get()`` that
    would raise if that invariant ever slipped. ``-updated_at`` makes the choice
    deterministic in that case instead of leaving it to the planner.
    """
    return (
        FlowExecution.objects.for_workspace(context.connection.workspace_id)
        .filter(
            contact=context.contact,
            status=ExecutionStatus.WAITING_REPLY,
            channel_connection=context.connection,
        )
        .order_by("-updated_at")
        .first()
    )


def default_reply_trigger_for(context: RoutingContext) -> Trigger | None:
    """The default-reply trigger covering this connection, lowest priority first.

    A stage rather than a competitor in :func:`~apps.flows.triggers.matching.match`,
    so it is queried here. Same candidate rules as every other type: bound to
    this connection or unbound, enabled, on a live flow.
    """
    from django.db.models import Exists, OuterRef, Q

    from apps.flows.models import FlowVersion
    from apps.flows.triggers.registry import spec_for

    published = FlowVersion.objects.unscoped().filter(flow_id=OuterRef("flow_id"), published=True)
    # .unscoped() with a reason (CONTRIBUTING.md): a correlated subquery inside
    # a query that is already scoped, compiled rather than executed on its own.
    candidates = (
        Trigger.objects.for_workspace(context.connection.workspace_id)
        .filter(type=TriggerType.DEFAULT_REPLY, enabled=True, flow__status=FlowStatus.ACTIVE)
        .filter(Q(channel_connection=context.connection) | Q(channel_connection__isnull=True))
        .filter(Exists(published))
        .select_related("flow")
    )
    spec = spec_for(TriggerType.DEFAULT_REPLY)
    platform = context.connection.platform
    for trigger in candidates:
        if trigger.channel_connection_id is None and (spec is None or platform not in spec.platforms):
            continue
        return trigger
    return None


def _start(context: RoutingContext, trigger: Trigger, variables: dict[str, Any]) -> bool:
    """Start the trigger's flow. False when it cannot run, which is not an error.

    ``FlowNotRunnableError`` here means a trigger points at a flow with no
    publishable version — a configuration problem five retries over six hours
    cannot fix, so it is logged and the event falls through to whatever comes
    next, exactly as ``apps/flows/handlers.py`` treats the same exception.
    """
    try:
        start_flow(
            context.contact,
            trigger.flow,
            started_by=StartedBy.stamp(StartedBy.TRIGGER, trigger.pk),
            variables=variables,
            connection=context.connection,
        )
    except FlowNotRunnableError as exc:
        logger.warning("Trigger %s cannot start flow %s: %s", trigger.pk, trigger.flow_id, exc)
        return False
    return True
