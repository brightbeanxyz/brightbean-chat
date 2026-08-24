"""The enrollment sweep — SPEC §12's ``SKIP LOCKED`` reconciler.

    Worker query: ``sequence_enrollment where status=active and next_run_at <=
    now`` batched via SKIP LOCKED.

**This is a reconciler, not the scheduler.** Subscribing and every advance
already enqueue the next step directly (:mod:`apps.campaigns.scheduling`), so a
healthy sequence never waits for this job — a step runs at the queue's next tick
rather than at the next hour. What the sweep is for is the row that went missing:
a queue row cancelled by mistake, an enqueue lost to a rollback that did not take
the enrollment with it, an enrollment edited straight in the admin. Without it,
such an enrollment would sit active and due for ever with nothing to notice.

Because every enqueue carries an idempotency key, re-enqueuing something already
queued returns the existing row — so the sweep is safe to run beside the normal
path, and safe to run from two workers at once. ``SKIP LOCKED`` is what keeps
those two workers off each other's rows rather than what keeps a step from
firing twice; the key does that.

Registered through ``apps.queueing.housekeeping.register_housekeeping_job``, so
this app adds a job rather than editing the hourly chain.
"""

import logging
from datetime import timedelta
from typing import Any

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.campaigns import services
from apps.campaigns.models import EnrollmentStatus, RuleTriggerFire, SequenceEnrollment
from apps.campaigns.rules import COOLDOWN
from apps.campaigns.scheduling import enqueue_step
from apps.queueing.housekeeping import register_housekeeping_job

__all__ = ["BATCH_SIZE", "MAX_BATCHES", "prune_rule_trigger_fires", "sweep_sequence_enrollments"]

logger = logging.getLogger(__name__)

#: Rows claimed per transaction. Matches the worker's own claim size (SPEC §15).
BATCH_SIZE = 50

#: A ceiling on one sweep, so a backlog is drained over several hours rather
#: than in one transaction-heavy burst that starves the rest of housekeeping.
MAX_BATCHES = 40

#: How far past ``COOLDOWN`` a cooldown row has to be before it is pruned. A row
#: deleted exactly at the boundary and re-inserted by a concurrent claim would
#: let through a fire the guard had just refused.
PRUNE_MARGIN = timedelta(minutes=5)


@register_housekeeping_job("sweep_sequence_enrollments")
def sweep_sequence_enrollments() -> str | None:
    """Re-enqueue every active enrollment that is due and has no queue row."""
    total = 0
    cursor: tuple[Any, Any] | None = None
    for _ in range(MAX_BATCHES):
        claimed, cursor = _sweep_batch(cursor)
        total += claimed
        if claimed < BATCH_SIZE:
            break
    else:
        logger.warning("Sequence sweep hit its %s-batch ceiling; the rest waits for the next run.", MAX_BATCHES)
    return f"re-enqueued {total} due enrollment(s)" if total else None


def _sweep_batch(cursor: tuple[Any, Any] | None) -> tuple[int, tuple[Any, Any] | None]:
    """One transaction's worth, starting after ``cursor``.

    **The cursor is not an optimisation.** The sweep does not change the rows it
    reads — the enqueue is idempotent by key and leaves ``next_run_at`` alone —
    so a plain ``LIMIT`` would hand every batch the same fifty rows and the loop
    would spin on them until it hit its ceiling. Keyset paging on
    ``(next_run_at, id)`` is what makes the second batch the *next* fifty.

    ``.unscoped()`` with a reason, per CONTRIBUTING.md: housekeeping runs across
    every tenant — that is what housekeeping *is* — and no ``for_workspace()``
    query can span them. Each row's own ``workspace`` is what the enqueue then
    scopes to, so nothing crosses a tenant boundary downstream.

    ``of=("self",)`` locks the enrollment rows and not the workspaces, contacts
    and sequences joined in for the enqueue. Without it a sweep would hold row
    locks on shared parent rows for the length of the batch.

    A row another sweeper holds is skipped rather than waited for, and its
    holder is the one enqueueing it — so two workers between them still cover
    everything, and neither blocks. A row held by something *else* is left for
    the next run, which is what a reconciler is for.
    """
    rows = SequenceEnrollment.objects.unscoped().filter(status=EnrollmentStatus.ACTIVE, next_run_at__lte=timezone.now())
    if cursor is not None:
        run_at, pk = cursor
        rows = rows.filter(Q(next_run_at__gt=run_at) | Q(next_run_at=run_at, pk__gt=pk))

    with transaction.atomic():
        due: list[Any] = list(
            rows.select_related("workspace", "contact", "sequence")
            .select_for_update(skip_locked=True, of=("self",))
            .order_by("next_run_at", "pk")[:BATCH_SIZE]
        )
        for enrollment in due:
            # A contact soft-deleted since the enrollment was created leaves it
            # active and due for ever: `activity.stand_down` cancelled its queue
            # row, and re-enqueueing hits the same idempotency key and gets that
            # cancelled row back. Retiring it here is what stops the sweep
            # re-examining it every hour until the end of time.
            if not services.retire_if_contact_gone(enrollment):
                enqueue_step(enrollment)

    if not due:
        return 0, cursor
    logger.debug("Sequence sweep re-enqueued %s enrollment(s).", len(due))
    last = due[-1]
    return len(due), (last.next_run_at, last.pk)


@register_housekeeping_job("prune_rule_trigger_fires")
def prune_rule_trigger_fires() -> str | None:
    """Drop cooldown rows that are past their window (:mod:`apps.campaigns.rules`).

    One row exists per (rule trigger, contact) that has ever fired, and it is
    only ever updated in place — so without this the table grows to the product
    of a workspace's contacts and its rule triggers and stays there, including
    rows for contacts deleted years ago. The guard only ever reads inside a
    60-second window, so anything older answers no question.

    Deleted in batches with a margin over ``COOLDOWN`` rather than exactly at it:
    a row deleted at the boundary and immediately re-inserted by a concurrent
    claim would let a fire through that the guard had just refused.
    """
    cutoff = timezone.now() - COOLDOWN - PRUNE_MARGIN
    total = 0
    for _ in range(MAX_BATCHES):
        # .unscoped() with a reason, per CONTRIBUTING.md: housekeeping runs
        # across every tenant, and a cooldown row's tenancy is not what decides
        # whether it is expired.
        stale = list(
            RuleTriggerFire.objects.unscoped()
            .filter(last_fired_at__lt=cutoff)
            .values_list("pk", flat=True)[:BATCH_SIZE]
        )
        if not stale:
            break
        # The cutoff is repeated on the delete, not just on the select. Between
        # the two statements `claim_rule_fire` can refresh one of these rows —
        # that is the *live* cooldown for a contact who just fired — and a
        # pk-only delete would remove it, letting the very next event through
        # inside the 60-second window the guard exists to hold.
        deleted, _ = RuleTriggerFire.objects.unscoped().filter(pk__in=stale, last_fired_at__lt=cutoff).delete()
        total += deleted
    return f"pruned {total} rule-trigger cooldown row(s)" if total else None
