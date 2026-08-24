"""When a step runs, and how it gets onto the queue (SPEC §12, §15).

Two decisions live here, and both are the kind that are invisible until they are
wrong.

**The delay is measured from the previous step's actual send.** SPEC §12 says a
step "waits its delay from the previous step's send", and
:attr:`SequenceEnrollment.last_sent_at` is that moment. The tempting alternative
— compute every step from the enrollment's start, or from what the *previous*
step was scheduled for — quietly compresses every later gap by however long the
worker was behind: a step due at 10:00 that ran at 10:40 would leave 23 h 20 m
before a "one day" successor. Measuring from the send makes the delay a floor,
which is what an operator writing "wait a day" means.

**Every enqueue carries an idempotency key.** ``schedule()`` returns the row
already holding a key rather than minting a second one, so the sweep in
:mod:`apps.campaigns.housekeeping` can re-enqueue anything it finds due without
ever double-firing a step. The key is
``seq:<enrollment id>:step:<position>``: the enrollment id is a UUIDv7 this
workspace owns, and re-enrollment mints a fresh one, so a contact who goes round
twice gets two distinct sets of keys.
"""

import logging
from datetime import datetime, timedelta
from typing import Any

from django.utils import timezone

from apps.campaigns.models import EnrollmentStatus, SequenceEnrollment, SequenceStep
from apps.common.windows import clock_for, into_window
from apps.queueing.models import ActionStatus, ActionType, ScheduledAction
from apps.queueing.registry import schedule

__all__ = ["cancel_pending_steps", "enqueue_step", "idempotency_key_for", "next_run_for"]

logger = logging.getLogger(__name__)


def next_run_for(step: SequenceStep, *, base: datetime, contact: Any, workspace: Any) -> datetime:
    """When ``step`` should run, given the moment its delay counts from.

    ``base + delay``, then moved forward into the step's send window. Forward
    only: a delay that lands outside the window waits for the window to open,
    and one that lands inside it fires there.
    """
    window = step.window
    run_at = base + timedelta(**{str(step.delay_unit): int(step.delay_value)})
    clock = clock_for(contact, workspace, use_contact_timezone=bool(window.get("use_contact_timezone")))
    return into_window(run_at, window, clock)


def idempotency_key_for(enrollment: SequenceEnrollment, position: int) -> str:
    """``seq:<enrollment>:step:<position>``. See the module docstring."""
    return f"seq:{enrollment.pk}:step:{position}"


def enqueue_step(enrollment: SequenceEnrollment) -> ScheduledAction | None:
    """Put this enrollment's next step on the queue. Idempotent.

    ``None`` when the enrollment has nothing due — a completed or unsubscribed
    row, or one with no ``next_run_at``. Callers may therefore hand it anything
    and let it decide, which is what lets the sweep and the step handler share
    one enqueue path.

    The row carries ``contact`` so the worker takes that contact's advisory lock
    before the handler runs (SPEC §9.6) — the engine's one-step-per-contact
    invariant depends on it.
    """
    if enrollment.status != EnrollmentStatus.ACTIVE or enrollment.next_run_at is None:
        return None
    return schedule(
        ActionType.SEQUENCE_STEP,
        enrollment.next_run_at,
        {"enrollment_id": str(enrollment.pk), "position": int(enrollment.current_step)},
        workspace=enrollment.workspace,
        contact=enrollment.contact_id,
        idempotency_key=idempotency_key_for(enrollment, enrollment.current_step),
    )


def cancel_pending_steps(enrollment: SequenceEnrollment) -> int:
    """Cancel queued steps for this enrollment. Returns how many.

    Matched on the payload rather than on the key, because the key names one
    position and an enrollment can legitimately have a stale row from an earlier
    one sitting behind it. ``pending`` only: a row a worker has already claimed
    is mid-flight, and SPEC §12 says a mid-flight execution completes.

    Scoped by ``for_workspace``, so a payload naming an enrollment id can never
    reach another tenant's queue rows even though the id alone would be enough
    to find them.
    """
    cancelled = (
        ScheduledAction.objects.for_workspace(enrollment.workspace_id)
        .filter(
            type=ActionType.SEQUENCE_STEP,
            status=ActionStatus.PENDING,
            payload__enrollment_id=str(enrollment.pk),
        )
        .update(status=ActionStatus.CANCELLED, updated_at=timezone.now())
    )
    if cancelled:
        logger.info("Cancelled %s queued step(s) for enrollment %s.", cancelled, enrollment.pk)
    return cancelled
