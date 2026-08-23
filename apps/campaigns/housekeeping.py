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
from typing import Any

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.campaigns.models import EnrollmentStatus, SequenceEnrollment
from apps.campaigns.scheduling import enqueue_step
from apps.queueing.housekeeping import register_housekeeping_job

__all__ = ["BATCH_SIZE", "MAX_BATCHES", "sweep_sequence_enrollments"]

logger = logging.getLogger(__name__)

#: Rows claimed per transaction. Matches the worker's own claim size (SPEC §15).
BATCH_SIZE = 50

#: A ceiling on one sweep, so a backlog is drained over several hours rather
#: than in one transaction-heavy burst that starves the rest of housekeeping.
MAX_BATCHES = 40


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
            enqueue_step(enrollment)

    if not due:
        return 0, cursor
    logger.debug("Sequence sweep re-enqueued %s enrollment(s).", len(due))
    last = due[-1]
    return len(due), (last.next_run_at, last.pk)
