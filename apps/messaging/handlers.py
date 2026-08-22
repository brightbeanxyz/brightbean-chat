"""The ``send_retry`` queue handler (SPEC §9.5).

    Node-level provider errors: 4xx permanent -> message failed [...] 429/5xx ->
    schedule send_retry with backoff 30s, 2m, 10m, 1h, 6h then failed.

Those five numbers are **not restated here**. They are
``apps.queueing.worker.BACKOFF_SCHEDULE``, and the ladder is walked through
``next_run_at`` — one definition, one place to change it, and no chance of this
app and the worker disagreeing about what "then failed" means.

--------------------------------------------------------------------------
Why the retry budget lives on the message
--------------------------------------------------------------------------

``ScheduledAction.attempts`` counts attempts *of the action*, and the first send
is inline with no action row at all — so an action-based budget is off by one
from birth, and a message whose first attempt failed inline would get one more
retry than one whose first attempt was itself a retry. ``Message.send_attempts``
counts provider calls, which is the thing SPEC §9.5 is actually bounding.

It also makes the handler safe to re-run: zombie recovery can re-run an action
whose handler committed, and a budget already spent means this returns rather
than calling anyone.

--------------------------------------------------------------------------
Why the retry re-checks compliance
--------------------------------------------------------------------------

Six hours is the last rung of the ladder, and a 24-hour messaging window can
close inside it. This is the compliance chokepoint (SPEC §19); a retry that
skipped it would be the one path in the product that sends outside a window
without asking.
"""

import logging
from datetime import timedelta
from typing import Any

from django.utils import timezone

from apps.messaging.codes import Denial, Failure
from apps.messaging.compliance import Allowed, can_send
from apps.messaging.models import Message, MessageStatus
from apps.queueing.models import DEFAULT_MAX_ATTEMPTS, ActionType, ScheduledAction
from apps.queueing.registry import register_handler, schedule
from apps.queueing.worker import next_run_at

logger = logging.getLogger(__name__)

__all__ = ["handle_send_retry", "schedule_send_retry"]

#: Cap on a platform's ``Retry-After``. A hostile or broken header must not be
#: able to park a message a month into the future.
MAX_RETRY_AFTER_SECONDS = 6 * 60 * 60


def schedule_send_retry(
    message: Message,
    *,
    delay_seconds: float | None = None,
    use_backoff: bool = True,
) -> ScheduledAction | None:
    """Arm the next attempt, or give up if the budget is spent.

    Two callers with two different needs.

    A **provider failure** walks the ladder: ``use_backoff`` is True and
    ``next_run_at(attempt)`` decides, with ``delay_seconds`` — a platform's
    ``Retry-After`` — acting as a floor rather than a replacement. A platform
    saying "come back in one second" while we are on the six-hour rung is not
    information worth acting on.

    A **rate deferral** is not a failure and does not walk the ladder: the token
    is due in ``delay_seconds`` and waiting thirty seconds for one due in two
    hundred milliseconds would turn a throttle into a stall. It also spends no
    budget, because no provider call happened.
    """
    attempt = max(message.send_attempts, 1)
    if use_backoff and attempt >= DEFAULT_MAX_ATTEMPTS:
        _give_up(message)
        return None

    now = timezone.now()
    delay = min(delay_seconds, MAX_RETRY_AFTER_SECONDS) if delay_seconds else 0.0
    if use_backoff:
        run_at = max(next_run_at(attempt), now + timedelta(seconds=delay))
    else:
        # A floor of one second: a bucket that says "now" would otherwise arm an
        # action the worker picks up before the token has actually accrued.
        run_at = now + timedelta(seconds=max(delay, 1.0))

    return schedule(
        ActionType.SEND_RETRY,
        run_at,
        {"message_id": str(message.pk)},
        # The Workspace instance, not its id: schedule() assigns straight to the
        # FK, and an id there raises. apps/notifications/queue.py hit this too.
        workspace=message.workspace,
        # The worker takes the contact advisory lock for any action naming a
        # contact, so this send cannot interleave with a flow step for the same
        # person (SPEC §9.6) for free.
        contact=message.conversation.contact_id,
        # Keyed on **when** rather than on the attempt number. Two callers
        # arming the same retry at the same moment collapse into one action,
        # which is what the key is for; but a *later* retry always gets a later
        # run_at and therefore a new key. An attempt-based key looks tidier and
        # deadlocks a throttled connection: a rate deferral spends no attempt,
        # so the second one would reuse the first one's key, ``schedule()``
        # would hand back the already-completed row, and the message would sit
        # queued forever with nothing scheduled to move it.
        idempotency_key=f"send_retry:{message.pk}:{int(run_at.timestamp())}",
        # The message's own key is what the provider call is deduplicated on and
        # stays constant for the whole logical send.
    )


def _give_up(message: Message) -> None:
    message.status = MessageStatus.FAILED
    message.error = Failure.RETRIES_EXHAUSTED.value
    message.save(update_fields=["status", "error", "updated_at"])


@register_handler(ActionType.SEND_RETRY, replace=True)
def handle_send_retry(payload: dict[str, Any], action: ScheduledAction) -> None:
    """Re-attempt one outbound message.

    Returns normally in every case the queue can do nothing about — a message
    that already reached a terminal status, one whose budget is spent, one
    compliance now refuses. Raising would retry work that cannot succeed, and
    ``replace=True`` on the registration is because ``ready()`` runs twice under
    some autoreload paths.
    """
    from apps.messaging.lookup import provider_message_id
    from apps.messaging.rendering import outbound_from_body
    from apps.messaging.services import _dispatch, _finalize, _identity_for

    message = _load(payload, action)
    if message is None or message.status != MessageStatus.QUEUED:
        return
    if message.send_attempts >= DEFAULT_MAX_ATTEMPTS:
        _give_up(message)
        return

    conversation = message.conversation
    connection = message.channel_connection
    identity = _identity_for(message.workspace_id, conversation.contact, connection)
    if identity is None:
        # NO_IDENTITY, not NO_ADAPTER: the contact has no address on this
        # channel, which is a different thing from the platform having no
        # adapter installed — and ``error`` is what an operator debugging a
        # stuck send reads, through codes.describe().
        _finalize(message, status=MessageStatus.FAILED, error=Denial.NO_IDENTITY.value)
        return

    if message.dispatched_at is not None and not message.provider_message_id:
        # SPEC §9.4's unknown outcome: the call went out and we never learned
        # what happened. Ask the platform before asking again.
        found = provider_message_id(connection, message)
        if found:
            _finalize(message, status=MessageStatus.SENT, provider_message_id=found)
            return
        logger.warning(
            "Re-sending message %s after an unknown outcome; %s offers no lookup, so a duplicate is "
            "possible (SPEC §9.4).",
            message.pk,
            connection.platform,
        )

    outbound = outbound_from_body(message.body)
    decision = can_send(identity, message.source, outbound)
    if not isinstance(decision, Allowed):
        _finalize(message, status=MessageStatus.FAILED, error=decision.code)
        return

    # Re-open the claim: this is a new, intended attempt rather than a racing
    # second caller, and _claim() only grants when dispatched_at is NULL.
    Message.objects.for_workspace(message.workspace_id).filter(pk=message.pk).update(
        dispatched_at=None, updated_at=timezone.now()
    )
    message.refresh_from_db()

    _dispatch(message, connection, identity, decision.apply(outbound), blocking=True)


def _load(payload: dict[str, Any], action: ScheduledAction) -> Message | None:
    """The message this action names, scoped to the action's own workspace."""
    message_id = payload.get("message_id")
    if not message_id or action.workspace_id is None:
        return None
    return (
        Message.objects.for_workspace(action.workspace_id)
        .filter(pk=message_id)
        .select_related("conversation", "channel_connection")
        .first()
    )
