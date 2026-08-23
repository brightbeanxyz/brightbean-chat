"""The only write path for sequences, steps and enrollments.

Every mutation goes through a function here rather than through the ORM
directly, for the reason ``apps/contacts/services.py`` gives for contacts: the
internal event catalog (:mod:`apps.campaigns.events`, ROADMAP contract 7) is
what rule triggers and outbound webhooks subscribe to, and a write that bypasses
this module is a change the rest of the product never learns about.

Three behaviours worth reading before calling anything:

**Re-enrollment restarts at step 1.** SPEC §12: subscribing a contact who is
already active retires the existing enrollment (status ``unsubscribed``, queued
steps cancelled) and creates a new one at position 1. The partial unique index
on ``(sequence, contact) WHERE status='active'`` means the database enforces
that rather than this function remembering to.

**Unsubscribing stops future steps; it does not stop the present one.** The
enrollment's status flips and its *pending* queue rows are cancelled. A step a
worker has already claimed, and any ``FlowExecution`` it started, runs to
completion — SPEC §12 says so in as many words, and reaching into a running
execution would mean this app knowing how to interrupt the engine.

**Subscribing to a sequence with no steps completes immediately.** There is
nothing to wait for, so the enrollment is created ``completed`` and the
``sequence.subscribed`` event still fires — the contact really was subscribed,
and a consumer counting subscriptions should not have to special-case an empty
campaign.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from apps.campaigns.errors import CampaignsError, SequenceNotRunnableError, WorkspaceMismatchError
from apps.campaigns.events import EVENT_SEQUENCE_SUBSCRIBED, EVENT_SEQUENCE_UNSUBSCRIBED, emit
from apps.campaigns.models import (
    DEFAULT_SEND_WINDOW,
    DelayUnit,
    EnrollmentStatus,
    Sequence,
    SequenceEnrollment,
    SequenceStatus,
    SequenceStep,
)
from apps.campaigns.scheduling import cancel_pending_steps, enqueue_step, next_run_for

__all__ = [
    "MAX_STEPS",
    "add_step",
    "create_sequence",
    "delete_sequence",
    "delete_step",
    "first_step",
    "move_step",
    "rename_sequence",
    "set_status",
    "step_at",
    "subscribe",
    "unsubscribe",
    "update_step",
]

logger = logging.getLogger(__name__)

#: A cap on rungs. Far past any real drip campaign, and it bounds the renumber
#: this module does on every reorder as well as the editor page's query.
MAX_STEPS = 50

MAX_NAME_CHARS = 200

#: The largest delay a step may carry, per unit. A step is not a calendar: "wait
#: 40 000 days" is a typo, and letting one through would park an enrollment past
#: the heat death of the campaign with nothing to say so.
MAX_DELAY: dict[str, int] = {DelayUnit.MINUTES: 60 * 24 * 90, DelayUnit.HOURS: 24 * 365, DelayUnit.DAYS: 365 * 2}


# ---------------------------------------------------------------------------
# Sequences
# ---------------------------------------------------------------------------


def create_sequence(workspace: Any, *, name: str) -> Sequence:
    """Create a sequence. Names are unique per workspace, case-insensitively."""
    cleaned = _clean_name(name)
    _assert_name_is_free(workspace, cleaned)
    sequence = Sequence(workspace=workspace, name=cleaned)
    with _unique_name():
        sequence.save()
    return sequence


def rename_sequence(sequence: Sequence, *, name: str) -> Sequence:
    cleaned = _clean_name(name)
    _assert_name_is_free(sequence.workspace_id, cleaned, excluding=sequence.pk)
    sequence.name = cleaned
    with _unique_name():
        sequence.save(update_fields=["name", "updated_at"])
    return sequence


def _assert_name_is_free(workspace: Any, name: str, *, excluding: Any = None) -> None:
    """Refuse a name another sequence in the workspace already holds.

    Matched case-insensitively, because the unique constraint is on
    ``Lower(name)`` — checking with ``=`` would let "onboarding" through and then
    let the database raise on it. The same shape ``apps/contacts/services.py``
    uses for tags and fields.
    """
    rows = Sequence.objects.for_workspace(workspace).filter(name__iexact=name)
    if excluding is not None:
        rows = rows.exclude(pk=excluding)
    if rows.exists():
        raise CampaignsError("A sequence with that name already exists.")


@contextmanager
def _unique_name() -> Iterator[None]:
    """Turn the unique-index violation the check above races with into a refusal.

    ``_assert_name_is_free`` is a check-then-write, so two concurrent requests
    can both pass it. Without this the loser gets an ``IntegrityError`` — a 500
    for input the single-threaded path answers with a readable message — and
    poisons any enclosing atomic block. The savepoint keeps that block usable.

    ``full_clean()`` does not cover this: the constraint is an expression over
    ``Lower(name)`` **and** ``workspace``, and a ``full_clean`` that excludes the
    derived ``workspace`` field skips every constraint naming it.
    """
    try:
        with transaction.atomic():
            yield
    except IntegrityError as exc:
        raise CampaignsError("A sequence with that name already exists.") from exc


def set_status(sequence: Sequence, *, status: str) -> Sequence:
    """Move a sequence between draft, active and archived.

    Only ``active`` accepts new enrollments (:func:`subscribe` enforces it), and
    nothing here touches the people already part-way through: pausing a campaign
    to edit it must not silently unsubscribe its subscribers.
    """
    if status not in SequenceStatus.values:
        raise CampaignsError("That is not a sequence status.")
    if status == SequenceStatus.ACTIVE and not sequence.steps.exists():
        raise SequenceNotRunnableError("Add at least one step before activating this sequence.")
    sequence.status = status
    sequence.save(update_fields=["status", "updated_at"])
    return sequence


@transaction.atomic
def delete_sequence(sequence: Sequence) -> None:
    """Delete a sequence, stopping everyone still walking it.

    The enrollments cascade, so their queued steps would otherwise be rows
    pointing at an enrollment id that no longer resolves — the handler drops
    those safely, but they would sit in the table until they came due. Cancelling
    them here keeps the queue honest about what is still going to happen.
    """
    for enrollment in SequenceEnrollment.objects.for_workspace(sequence.workspace_id).filter(
        sequence=sequence, status=EnrollmentStatus.ACTIVE
    ):
        cancel_pending_steps(enrollment)
    sequence.delete()


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def add_step(
    sequence: Sequence, *, flow: Any, delay_value: Any, delay_unit: str, send_window: Any = None
) -> SequenceStep:
    """Append a step to the end of the sequence.

    ``delay_value`` is deliberately untyped: it comes off a form as a string and
    ``_clean_delay`` is the one place that decides what a delay may be. Typing it
    ``int`` here would push that parse out to every caller.
    """
    if flow.workspace_id != sequence.workspace_id:
        raise WorkspaceMismatchError("That flow belongs to a different workspace than the sequence.")
    count = sequence.steps.count()
    if count >= MAX_STEPS:
        raise CampaignsError(f"A sequence may have at most {MAX_STEPS} steps.")
    step = SequenceStep(
        workspace_id=sequence.workspace_id,
        sequence=sequence,
        position=count + 1,
        flow=flow,
        delay_value=_clean_delay(delay_value, delay_unit),
        delay_unit=delay_unit,
        send_window=_clean_window(send_window),
    )
    step.full_clean(exclude=["workspace"])
    step.save()
    return step


def update_step(
    step: SequenceStep,
    *,
    flow: Any = None,
    delay_value: Any = None,
    delay_unit: str | None = None,
    send_window: Any = None,
) -> SequenceStep:
    """Edit one step in place. Enrollments already past it are unaffected."""
    if flow is not None:
        if flow.workspace_id != step.workspace_id:
            raise WorkspaceMismatchError("That flow belongs to a different workspace than the sequence.")
        step.flow = flow
    if delay_unit is not None:
        step.delay_unit = delay_unit
    if delay_value is not None:
        step.delay_value = _clean_delay(delay_value, step.delay_unit)
    if send_window is not None:
        step.send_window = _clean_window(send_window)
    step.full_clean(exclude=["workspace"])
    step.save(update_fields=["flow", "delay_value", "delay_unit", "send_window", "updated_at"])
    return step


@transaction.atomic
def delete_step(step: SequenceStep) -> None:
    """Remove a step and close the gap it leaves in the numbering.

    Enrollments are **not** renumbered with it. One sitting at the deleted
    position now points at whatever moved up into it, which is the reading an
    operator expects ("the step I removed does not run"), and one sitting past it
    is shifted one rung earlier — accepted, and the reason
    :func:`apps.campaigns.handlers.handle_sequence_step` re-reads the step by
    position rather than trusting a stored foreign key.
    """
    sequence_id, position = step.sequence_id, step.position
    step.delete()
    SequenceStep.objects.for_workspace(step.workspace_id).filter(sequence_id=sequence_id, position__gt=position).update(
        position=F("position") - 1, updated_at=timezone.now()
    )


@transaction.atomic
def move_step(step: SequenceStep, *, direction: str) -> SequenceStep:
    """Swap a step with its neighbour. ``direction`` is ``up`` or ``down``.

    Two updates through a parking position rather than one, because
    ``(sequence, position)`` is unique and the intermediate state of a naive swap
    violates it. The same shape ``apps/flows/triggers/services.py::move_trigger``
    uses for trigger priorities.
    """
    if direction not in {"up", "down"}:
        raise CampaignsError("A step moves up or down.")
    offset = -1 if direction == "up" else 1
    neighbour = (
        SequenceStep.objects.for_workspace(step.workspace_id)
        .filter(sequence_id=step.sequence_id, position=step.position + offset)
        .first()
    )
    if neighbour is None:
        return step

    parked, target = step.position, neighbour.position
    SequenceStep.objects.for_workspace(step.workspace_id).filter(pk=step.pk).update(position=0)
    SequenceStep.objects.for_workspace(step.workspace_id).filter(pk=neighbour.pk).update(position=parked)
    SequenceStep.objects.for_workspace(step.workspace_id).filter(pk=step.pk).update(position=target)
    step.refresh_from_db()
    return step


def step_at(sequence: Sequence, position: int) -> SequenceStep | None:
    """The step at ``position``, or ``None`` past the end of the sequence."""
    return (
        SequenceStep.objects.for_workspace(sequence.workspace_id)
        .filter(sequence=sequence, position=position)
        .select_related("flow")
        .first()
    )


def first_step(sequence: Sequence) -> SequenceStep | None:
    return step_at(sequence, 1)


# ---------------------------------------------------------------------------
# Enrollment
# ---------------------------------------------------------------------------


@transaction.atomic
def subscribe(sequence: Sequence, contact: Any, *, source: str = "manual") -> SequenceEnrollment:
    """Enroll ``contact`` in ``sequence``, restarting from step 1.

    ``source`` is a short vocabulary word for the log line — ``manual``,
    ``flow``, ``rule``, ``api``. It is deliberately *not* in the event payload:
    contract 7 payloads carry ids, and "how somebody was enrolled" is a fact the
    database already holds against the enrollment's own creation.
    """
    if contact.workspace_id != sequence.workspace_id:
        raise WorkspaceMismatchError("That contact belongs to a different workspace than the sequence.")
    if sequence.status == SequenceStatus.ARCHIVED:
        raise SequenceNotRunnableError("That sequence is archived.")

    _retire_active(sequence, contact)

    step = first_step(sequence)
    now = timezone.now()
    enrollment = SequenceEnrollment(
        sequence=sequence,
        contact=contact,
        current_step=1,
        status=EnrollmentStatus.ACTIVE if step is not None else EnrollmentStatus.COMPLETED,
        next_run_at=(
            next_run_for(step, base=now, contact=contact, workspace=sequence.workspace) if step is not None else None
        ),
    )
    enrollment.save()

    enqueue_step(enrollment)
    emit(
        EVENT_SEQUENCE_SUBSCRIBED,
        workspace_id=sequence.workspace_id,
        contact_id=contact.pk,
        sequence_id=sequence.pk,
        enrollment_id=enrollment.pk,
    )
    logger.info("Contact %s subscribed to sequence %s (%s).", contact.pk, sequence.pk, source)
    return enrollment


@transaction.atomic
def unsubscribe(sequence: Sequence, contact: Any) -> SequenceEnrollment | None:
    """Stop ``contact``'s walk through ``sequence``. ``None`` if they were not on it.

    Idempotent, and idempotent *quietly*: a contact who is not enrolled produces
    no event. The event means "this enrollment stopped early", and re-sending it
    for a no-op would turn a re-run flow into a duplicate webhook delivery — and,
    since ``sequence_unsubscribed`` is a rule-trigger event, into a loop.
    """
    enrollment = (
        SequenceEnrollment.objects.for_workspace(sequence.workspace_id)
        .filter(sequence=sequence, contact=contact, status=EnrollmentStatus.ACTIVE)
        .first()
    )
    if enrollment is None:
        return None
    _retire(enrollment)
    emit(
        EVENT_SEQUENCE_UNSUBSCRIBED,
        workspace_id=sequence.workspace_id,
        contact_id=contact.pk,
        sequence_id=sequence.pk,
        enrollment_id=enrollment.pk,
    )
    return enrollment


def _retire_active(sequence: Sequence, contact: Any) -> None:
    """Retire whatever active enrollment stands in the way of a fresh one.

    Silent — no ``sequence.unsubscribed`` event. Re-enrollment is one act, and
    announcing an unsubscribe the operator never asked for would fire every
    ``sequence_unsubscribed`` rule trigger in the workspace every time somebody
    re-ran an onboarding flow.
    """
    existing = (
        SequenceEnrollment.objects.for_workspace(sequence.workspace_id)
        .filter(sequence=sequence, contact=contact, status=EnrollmentStatus.ACTIVE)
        .first()
    )
    if existing is not None:
        _retire(existing)


def _retire(enrollment: SequenceEnrollment) -> None:
    enrollment.status = EnrollmentStatus.UNSUBSCRIBED
    enrollment.next_run_at = None
    enrollment.save(update_fields=["status", "next_run_at", "updated_at"])
    cancel_pending_steps(enrollment)


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


def _clean_name(name: Any) -> str:
    cleaned = (name or "").strip() if isinstance(name, str) else ""
    if not cleaned:
        raise CampaignsError("Give the sequence a name.")
    if len(cleaned) > MAX_NAME_CHARS:
        raise CampaignsError(f"That name is too long ({MAX_NAME_CHARS} characters maximum).")
    return cleaned


def _clean_delay(value: Any, unit: Any) -> int:
    if unit not in DelayUnit.values:
        raise CampaignsError("Pick minutes, hours or days.")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise CampaignsError("The delay must be a whole number.") from exc
    if number < 0:
        raise CampaignsError("The delay cannot be negative.")
    if number > MAX_DELAY[unit]:
        raise CampaignsError(f"That delay is too long (at most {MAX_DELAY[unit]} {unit}).")
    return number


def _clean_window(raw: Any) -> dict[str, Any]:
    """Keep only the five keys SPEC §11.5 names, in the types it names them in.

    An allowlist rather than a passthrough: ``send_window`` is a JSON column fed
    from a form, and storing whatever arrived would be the mass-assignment hole
    ``apps/flows/schema/fields.py`` closes structurally for graph configs
    (SECURITY-BASELINE §7).
    """
    from apps.common.windows import WEEKDAYS

    if not isinstance(raw, dict):
        return dict(DEFAULT_SEND_WINDOW)
    days = raw.get("days")
    return {
        "enabled": bool(raw.get("enabled")),
        "days": [str(day).lower() for day in days if str(day).lower() in WEEKDAYS] if isinstance(days, list) else [],
        "from": _clean_time(raw.get("from"), "09:00"),
        "to": _clean_time(raw.get("to"), "17:00"),
        "use_contact_timezone": bool(raw.get("use_contact_timezone")),
    }


def _clean_time(raw: Any, fallback: str) -> str:
    from datetime import time

    if isinstance(raw, str):
        try:
            return time.fromisoformat(raw).isoformat(timespec="minutes")
        except ValueError:
            pass
    return fallback
