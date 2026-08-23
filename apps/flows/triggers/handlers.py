"""The worker half of routing: hand an event off, and pick it back up.

SPEC §7.1 writes the fallback as ``scheduled_action(type=resume_execution)``, and
that action cannot express this one. ``handle_resume_execution`` resumes an
execution *by handle*, and the handle is derived **from the event** by
``apps.flows.engine.waits`` — so a contended or over-budget resume has nothing to
put in the payload, and pre-computing the handle outside the lock would mean a
second copy of the engine's answer-matching. ``START_FLOW`` covers only the case
where a trigger has already been matched.

``apps.queueing.models.ActionType`` is documented as **not a closed set** — "the
authority on what may be processed is the handler registry" — so this registers
its own type rather than editing a lower layer's enum and shipping an
``AlterField`` in that app's migrations.

What the handler re-runs is *the same pipeline*, from a named stage. One
implementation, two callers: L5-D's and L6-C's hooks then run on the deferred
path for free, and the stage semantics cannot drift between inline and worker.
"""

import hashlib
import logging
from typing import Any

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.flows.compat import installed_model
from apps.flows.models import RoutedEvent
from apps.flows.triggers.budget import InlineBudget
from apps.flows.triggers.hooks import Stage
from apps.flows.triggers.serialization import event_to_payload, payload_to_event, shrink_to_fit
from apps.queueing.registry import IdempotencyKeyConflictError, register_handler, schedule

__all__ = ["ROUTE_EVENT", "handle_route_event", "hand_off", "route_idempotency_key"]

logger = logging.getLogger(__name__)

#: The action type this issue adds. A local constant, not an enum member: see the
#: module docstring, and ``ActionType``'s own.
ROUTE_EVENT = "route_event"


def route_idempotency_key(connection: Any, provider_event_id: str, stage: str) -> str:
    """``route:<connection>:<stage>:<digest>`` — built only from ids we own.

    Hashed rather than sliced. The provider's id is attacker-controlled and
    unbounded, so putting it in the column raw is both a size problem and a way
    to collide two long ids that share a prefix. ``(connection, provider_event_id)``
    is already unique upstream in ``webhook_event_log``, so the digest identifies
    the event exactly.
    """
    digest = hashlib.sha256(provider_event_id.encode("utf-8")).hexdigest()[:40]
    return f"route:{connection.pk}:{stage}:{digest}"


def hand_off(context: Any, stage: Stage, *, reason: str = "") -> None:
    """Give the rest of this event's routing to the worker.

    A no-op on the worker path: the worker *is* the fallback, and a handler that
    re-enqueued itself on a lock it already holds would be a loop.
    """
    from apps.flows.triggers.context import RoutingMode

    if context.mode is RoutingMode.WORKER:
        logger.debug("Worker routing asked to hand off at %s (%s); already there.", stage, reason)
        return

    payload = shrink_to_fit(
        {
            "stage": str(stage),
            "connection_id": str(context.connection.pk),
            "event": event_to_payload(context.event),
        }
    )
    if payload is None:
        logger.warning(
            "A %s event on connection %s is too large to queue; dropping it.",
            context.event.type,
            context.connection.pk,
        )
        return

    try:
        schedule(
            ROUTE_EVENT,
            timezone.now(),
            payload,
            workspace=context.workspace,
            contact=context.contact,
            idempotency_key=route_idempotency_key(context.connection, context.event.provider_event_id, str(stage)),
        )
    except IdempotencyKeyConflictError:
        # Only raised for a key held by *another* workspace, which would mean a
        # key built from something we do not own. Noisy on purpose.
        logger.exception(
            "route_event idempotency key collided across workspaces on connection %s",
            context.connection.pk,
        )
        return
    logger.debug("Handed a %s event to the worker at stage %s (%s).", context.event.type, stage, reason)


@register_handler(ROUTE_EVENT)
def handle_route_event(payload: dict[str, Any], action: Any) -> None:
    """Finish routing an event the request could not, under the blocking lock.

    Payload: ``stage``, ``connection_id``, ``event`` (see
    :mod:`apps.flows.triggers.serialization`).
    """
    from apps.flows.triggers.context import RoutingMode, build_context
    from apps.flows.triggers.pipeline import route_one

    connection = _connection(action.workspace_id, payload.get("connection_id"))
    if connection is None:
        logger.warning("route_event action %s names a connection that is gone; dropping it.", action.pk)
        return

    event = payload_to_event(payload.get("event"), connection)
    if event is None:
        logger.warning("route_event action %s carries no usable event; dropping it.", action.pk)
        return

    try:
        stage = Stage(str(payload.get("stage") or ""))
    except ValueError:
        logger.warning("route_event action %s names stage %r; dropping it.", action.pk, str(payload.get("stage"))[:40])
        return

    if not _claim(connection, event, stage):
        logger.info("route_event action %s was already applied; skipping.", action.pk)
        return

    context = build_context(connection, event, InlineBudget.unbounded(), mode=RoutingMode.WORKER)
    if context is None:
        return
    route_one(context, from_stage=stage)


def _claim(connection: Any, event: Any, stage: Stage) -> bool:
    """Record that this event has been routed. False when it already was.

    The handler's first act, and it commits with the work — which is the whole
    design. ``apps.queueing.worker.process_action`` runs the handler and marks
    the row done in one transaction, so a failure rolls this back with it and the
    retry legitimately re-runs. What it protects against is the other case:
    zombie recovery resets a ``running`` row after ten minutes, so a slow handler
    can be claimed a second time while the first is still in flight. Both take
    the blocking contact lock and serialise, and the second would otherwise
    re-run the pipeline over the first's committed work — calling ``start_flow``
    again, minting a *new* execution id, and so a *new* outbound idempotency key.
    ``send_outbound``'s own key cannot see that (SPEC §9.4 keys it on the
    execution), so this is the row that makes SPEC §21's "zero duplicate sends
    across 1k forced worker retries" true on the deferred path.
    """
    from apps.flows import messaging as messaging_facade

    try:
        with transaction.atomic():
            RoutedEvent(
                workspace_id=connection.workspace_id,
                channel_connection=connection,
                # Hashed rather than sliced when over-long, so two events
                # sharing a 200-character prefix cannot claim one another's
                # guard. Idempotent, so an id already bounded on its way into
                # the payload passes through unchanged.
                provider_event_id=messaging_facade.bounded_identifier(
                    event.provider_event_id, limit=_MAX_EVENT_ID_CHARS
                ),
                stage=str(stage),
            ).save()
        return True
    except IntegrityError:
        return False


def _connection(workspace_id: Any, raw_id: Any) -> Any | None:
    """Re-resolve the connection inside the workspace that owns the action.

    The same rule ``apps/flows/handlers.py`` states: a payload is a document that
    has been sitting in a table, so its ids are ids and not trusted objects.
    """
    model = installed_model("channels", "apps.channels", "ChannelConnection")
    if model is None or not raw_id:  # pragma: no cover - channels is always installed
        return None
    try:
        return model.objects.for_workspace(workspace_id).filter(pk=raw_id).first()
    except (ValueError, TypeError, ValidationError):
        # A malformed uuid in a stored payload is a dropped action, not a 500 in
        # the worker loop that would retry it four more times to the same end.
        return None


#: ``RoutedEvent.provider_event_id``'s column width.
_MAX_EVENT_ID_CHARS = 200
