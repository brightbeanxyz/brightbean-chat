"""The ``sequence_step`` queue handler (SPEC §5, §12, §15).

``ActionType.SEQUENCE_STEP`` is a name ``apps.queueing.models`` already reserved
for this issue; registration is an import side effect of
``CampaignsConfig.ready()``, which is the pattern
:mod:`apps.queueing.registry`'s docstring writes out.

Three things the queue guarantees, so nothing here re-does them:

* the handler runs **inside a transaction that already holds the contact
  advisory lock**, because the row names a contact — which is what makes
  starting a flow from here safe against the inline webhook path;
* raising retries the row on SPEC §15's backoff ladder;
* returning normally marks it done, in the same transaction as the work.

So the interesting decisions here are the ones where the handler *declines* to
raise. A row can arrive for an enrollment somebody unsubscribed an hour ago, for
a position the enrollment has already passed, or for a step whose flow was
unpublished in the meantime. None of those is retriable — five attempts over six
hours cannot republish a flow — and none of them is a failure of the queue, so
each is logged and swallowed. The one thing that must **not** happen is a
sequence stalling silently: a step whose flow will not run still advances the
enrollment, so the rest of the campaign continues.
"""

import logging
from typing import Any
from uuid import UUID

from django.utils import timezone

from apps.campaigns.models import EnrollmentStatus, SequenceEnrollment
from apps.campaigns.scheduling import enqueue_step, next_run_for
from apps.campaigns.services import step_at
from apps.flows.engine import FlowNotRunnableError, start_flow
from apps.flows.models import StartedBy
from apps.flows.triggers.entrypoints import connection_for_contact
from apps.queueing.models import ActionType, ScheduledAction
from apps.queueing.registry import register_handler

__all__ = ["handle_sequence_step"]

logger = logging.getLogger(__name__)


@register_handler(ActionType.SEQUENCE_STEP)
def handle_sequence_step(payload: dict[str, Any], action: ScheduledAction) -> None:
    """Run one rung of a sequence and arrange the next.

    Payload: ``enrollment_id`` and ``position``.

    Everything is re-resolved from ids inside the workspace that owns the row. A
    payload is a document that has been sitting in a table, possibly for weeks,
    so treating its ids as ids rather than as trusted objects is what keeps a
    scheduled action from reaching across tenants.
    """
    enrollment = _enrollment(action.workspace_id, payload.get("enrollment_id"))
    if enrollment is None:
        logger.info("sequence_step action %s names an enrollment that is gone; dropping it.", action.pk)
        return
    if enrollment.status != EnrollmentStatus.ACTIVE:
        # Unsubscribed or completed between the enqueue and now. `unsubscribe`
        # cancels pending rows, so this is the narrower race where a worker had
        # already claimed one — and SPEC §12's "unsubscribe stops future steps"
        # is exactly this check.
        logger.info(
            "sequence_step action %s: enrollment %s is %s; dropping it.", action.pk, enrollment.pk, enrollment.status
        )
        return

    position = _position(payload)
    if position != enrollment.current_step:
        # A stale duplicate: the enrollment has moved on. Idempotency keys stop
        # this being common, but zombie recovery re-runs a handler whose work
        # committed, and that is precisely the case this guard is for.
        logger.info(
            "sequence_step action %s is for position %s but enrollment %s is at %s; dropping it.",
            action.pk,
            position,
            enrollment.pk,
            enrollment.current_step,
        )
        return

    step = step_at(enrollment.sequence, position)
    if step is not None:
        _start(enrollment, step)
    else:
        # The step was deleted while this row waited. Nothing to send; the
        # enrollment still has to move, or it would sit due for ever.
        logger.info(
            "sequence_step action %s: step %s of sequence %s is gone.", action.pk, position, enrollment.sequence_id
        )

    _advance(enrollment, sent_at=timezone.now())


def _start(enrollment: SequenceEnrollment, step: Any) -> None:
    """Start the step's flow for the contact, or log why it could not.

    ``FlowNotRunnableError`` is swallowed rather than raised: an unpublished or
    archived step flow is a configuration problem, and retrying it five times
    over six hours would neither fix it nor let the rest of the campaign run.
    """
    try:
        start_flow(
            enrollment.contact,
            step.flow,
            started_by=StartedBy.stamp(StartedBy.SEQUENCE, enrollment.sequence_id),
            variables={"sequence_id": str(enrollment.sequence_id), "sequence_step": step.position},
            connection=connection_for_contact(enrollment.contact),
        )
    except FlowNotRunnableError as exc:
        logger.warning(
            "Sequence %s step %s cannot start flow %s for contact %s: %s",
            enrollment.sequence_id,
            step.position,
            step.flow_id,
            enrollment.contact_id,
            exc,
        )


def _advance(enrollment: SequenceEnrollment, *, sent_at: Any) -> None:
    """Move to the next rung, or complete.

    The next ``next_run_at`` is computed from ``sent_at`` — the moment this step
    actually ran — rather than from what it was scheduled for. See
    :mod:`apps.campaigns.scheduling`: the other reading lets worker lag compress
    every later gap in the campaign.
    """
    following = step_at(enrollment.sequence, enrollment.current_step + 1)
    enrollment.last_sent_at = sent_at
    enrollment.current_step += 1
    if following is None:
        enrollment.status = EnrollmentStatus.COMPLETED
        enrollment.next_run_at = None
    else:
        enrollment.next_run_at = next_run_for(
            following, base=sent_at, contact=enrollment.contact, workspace=enrollment.workspace
        )
    enrollment.save(update_fields=["current_step", "next_run_at", "last_sent_at", "status", "updated_at"])
    enqueue_step(enrollment)


def _enrollment(workspace_id: Any, raw_id: Any) -> SequenceEnrollment | None:
    if not raw_id or workspace_id is None:
        return None
    try:
        pk = UUID(str(raw_id))
    except (ValueError, AttributeError, TypeError):
        return None
    return (
        SequenceEnrollment.objects.for_workspace(workspace_id)
        .filter(pk=pk)
        .select_related("sequence", "contact", "workspace")
        .first()
    )


def _position(payload: dict[str, Any]) -> int:
    """The rung this row is for. ``-1`` for a payload that names none, which no
    enrollment's ``current_step`` can equal — so a malformed row is dropped."""
    try:
        return int(payload["position"])
    except (KeyError, TypeError, ValueError):
        return -1
