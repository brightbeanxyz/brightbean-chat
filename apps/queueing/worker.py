"""Claiming, running and rescheduling actions — the worker's whole brain.

Every entry point shares this module: ``manage.py process_tasks`` loops over
:func:`run_batch`, ``manage.py tick`` and ``/internal/tick`` both call
:func:`drain`. There is one implementation of "what happens to a due row", so
the three cannot drift.

**Concurrency safety is structural, not conventional.** SPEC §15's claim
statement is the load-bearing part::

    UPDATE scheduled_action SET status='running', attempts=attempts+1
     WHERE id IN (SELECT id FROM scheduled_action
                   WHERE status='pending' AND run_at <= now()
                   ORDER BY run_at LIMIT 50 FOR UPDATE SKIP LOCKED)
    RETURNING *

``FOR UPDATE SKIP LOCKED`` means two workers running the identical statement at
the identical moment select disjoint row sets — the second does not block and
does not see the first's rows. The ``UPDATE`` commits the ``running`` status
before any handler runs, so a third worker arriving a millisecond later sees no
``pending`` row to claim. No queue table lock, no leader election, no Redis
(SPEC §22). ``/internal/tick`` is therefore safe to fire while workers run, and
two workers plus a tick over the same backlog is a supported configuration, not
a race to be avoided.

**Crossing tenants is the point here.** ``ScheduledAction`` is a
``WorkspaceScopedModel``, and every *application* read of it must go through
``.for_workspace()``. The worker is the deliberate exception: it drains the
whole deployment, tenant rows and NULL-workspace system rows alike. The claim
runs through ``.raw()``, which is not a ``WorkspaceScopedQuerySet`` and so is
not guarded at all — that is why it lives in this one function, with this
comment, and why every other cross-tenant read in the app spells ``.unscoped()``
out loud (CONTRIBUTING.md, "``.unscoped()`` needs a comment saying why").
"""

import logging
import time
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.common.logging import scrub
from apps.queueing.locks import contact_lock
from apps.queueing.models import ActionStatus, ScheduledAction
from apps.queueing.registry import get_handler, registered_types

__all__ = [
    "BACKOFF_SCHEDULE",
    "DEFAULT_BATCH_SIZE",
    "BatchResult",
    "claim_batch",
    "drain",
    "next_run_at",
    "positive_int",
    "process_action",
    "run_batch",
]

logger = logging.getLogger(__name__)

#: SPEC §15 / §9.5: 30s, 2m, 10m, 1h, 6h, then failed.
#:
#: Indexed by ``attempts - 1`` (the claim has already incremented ``attempts``
#: by the time a failure is recorded) and clamped to the last entry, so a row
#: with a raised ``max_attempts`` keeps retrying every 6 hours rather than
#: falling off the end of the tuple.
#:
#: Worth knowing when reading a failed row: with the default ``max_attempts=5``
#: an action gets five attempts and therefore only the first four delays. The
#: 6h step is reached only where ``max_attempts`` is 6 or more.
BACKOFF_SCHEDULE: tuple[int, ...] = (30, 2 * 60, 10 * 60, 60 * 60, 6 * 60 * 60)

DEFAULT_BATCH_SIZE = 50

#: A stored error is rendered in the admin and read by humans; an exception
#: carrying a hostile payload should not be able to write a novel into a TEXT
#: column on every one of its five attempts.
MAX_STORED_ERROR_CHARS = 2000

# The table name is written out rather than interpolated from
# ``_meta.db_table``: an f-string here would be a string-built query (ruff S608)
# for no benefit, and the two are pinned together by a test.
CLAIM_SQL = """
    UPDATE queueing_scheduled_action
       SET status = 'running',
           attempts = attempts + 1,
           updated_at = now()
     WHERE id IN (
           SELECT id
             FROM queueing_scheduled_action
            WHERE status = 'pending'
              AND run_at <= now()
            ORDER BY run_at
            LIMIT %s
              FOR UPDATE SKIP LOCKED
           )
    RETURNING *
"""


@dataclass
class BatchResult:
    """What one batch (or one drain) did. Returned to callers and to operators."""

    claimed: int = 0
    done: int = 0
    failed: int = 0
    retried: int = 0
    #: Claimed, then left in ``running`` because even recording the failure
    #: failed. Counted apart from ``failed`` so the totals reconcile against
    #: the table: a stranded row is still ``running`` until zombie recovery
    #: takes it, and reporting it as ``failed`` sends an operator looking for
    #: a terminal row that is not there.
    stranded: int = 0

    def __iadd__(self, other: "BatchResult") -> "BatchResult":
        self.claimed += other.claimed
        self.done += other.done
        self.failed += other.failed
        self.retried += other.retried
        self.stranded += other.stranded
        return self


def next_run_at(attempts: int, now: datetime | None = None) -> datetime:
    """When a row that has failed ``attempts`` times should next be tried.

    A pure function of the attempt count so the schedule can be asserted
    exactly, without freezing the clock or taking a dependency to do it.
    """
    now = now or timezone.now()
    index = min(max(attempts, 1), len(BACKOFF_SCHEDULE)) - 1
    return now + timedelta(seconds=BACKOFF_SCHEDULE[index])


def positive_int(raw: str | int) -> int:
    """Parse a count that must be at least 1.

    Doubles as an argparse ``type=``: argparse turns a ``ValueError`` from a
    converter into a proper usage error, so the commands get the check for
    free.

    A batch size of 0 is the reason this exists. It reads as "do nothing", but
    every loop here decides it has drained the queue by comparing
    ``claimed < batch_size`` — and ``0 < 0`` is false, so a zero batch size
    turned the drain into a spin that claimed nothing and never stopped.
    """
    value = int(raw)
    if value < 1:
        raise ValueError(f"must be 1 or greater, got {value}")
    return value


def claim_batch(limit: int = DEFAULT_BATCH_SIZE) -> list[ScheduledAction]:
    """Atomically claim up to ``limit`` due rows and return them.

    Must run in autocommit — outside any ``atomic()`` block — so the claim is
    committed (and therefore invisible to other workers) before the first
    handler runs. A claim rolled back with its handler would be a lost update
    under exactly the concurrency this whole design is for.

    ``updated_at`` is set explicitly. Django applies ``auto_now`` in
    ``Model.save()`` and nowhere else — not in ``QuerySet.update()`` and
    certainly not in raw SQL — and zombie recovery keys off ``updated_at``, so
    omitting it would make every row claimed by a healthy worker look abandoned
    ten minutes later.
    """
    limit = positive_int(limit)
    # Cross-tenant on purpose: this is the deployment-wide drain. See the module
    # docstring. Application code reads ScheduledAction through .for_workspace().
    claimed = list(ScheduledAction.objects.raw(CLAIM_SQL, [limit]))
    # The subquery orders by run_at, but RETURNING has no guaranteed order, so
    # without this the *oldest* row in a batch could be processed last. Sorting
    # in Python keeps the claim statement the one SPEC §15 writes down.
    claimed.sort(key=lambda action: action.run_at)
    return claimed


def _storable(text: str) -> str:
    """Scrub and cap anything on its way into ``last_error``.

    Applied in ``_record_failure`` rather than at each call site, so the
    invariant "everything in that column has been scrubbed and bounded" holds
    for every writer including the ones not written yet. Scrubbed because
    ``last_error`` is a plain column shown in the admin and an exception's
    ``str()`` routinely carries the URL, header or token that caused it
    (SECURITY-BASELINE §5); capped because a hostile payload should not be able
    to write a novel into a TEXT column on each of its attempts.
    """
    text = scrub(text)
    if len(text) > MAX_STORED_ERROR_CHARS:
        text = text[: MAX_STORED_ERROR_CHARS - 1] + "…"
    return text


def _error_text(exc: BaseException) -> str:
    """Render a handler failure. Scrubbing and capping happen on write."""
    return f"{type(exc).__name__}: {exc}"


def _mark_done(action: ScheduledAction) -> None:
    action.status = ActionStatus.DONE
    action.last_error = ""
    action.save(update_fields=["status", "last_error", "updated_at"])


def _record_failure(action: ScheduledAction, message: str, *, permanent: bool) -> str:
    """Send a failed row back to pending with backoff, or terminally fail it.

    Runs in its own transaction: the caller's has been rolled back, taking the
    handler's partial writes with it, and this update must survive.
    """
    action.last_error = _storable(message)
    if permanent or action.attempts >= action.max_attempts:
        action.status = ActionStatus.FAILED
    else:
        action.status = ActionStatus.PENDING
        action.run_at = next_run_at(action.attempts)

    with transaction.atomic():
        action.save(update_fields=["status", "run_at", "last_error", "updated_at"])
    return str(action.status)


def process_action(action: ScheduledAction) -> str:
    """Run one claimed action. Returns its resulting status.

    The handler runs inside a transaction that already holds the contact
    advisory lock when the row names a contact (SPEC §9.6: claim, then lock,
    then touch the execution). Marking the row ``done`` is part of that same
    transaction, so "the work happened but the row still says running" is only
    reachable by a process dying mid-transaction — which is precisely what
    zombie recovery exists to clean up.
    """
    handler = get_handler(action.type)
    if handler is None:
        message = (
            f"No handler registered for action type {action.type!r}. "
            f"Registered types: {', '.join(registered_types()) or 'none'}."
        )
        logger.error("Queue action %s has no handler (type=%s)", action.pk, action.type)
        # Permanent: retrying an unregistered type five times over six hours
        # cannot succeed, and the row is more useful as a visible failure.
        return _record_failure(action, message, permanent=True)

    started = time.monotonic()
    lock: AbstractContextManager[Any] = contact_lock(action.contact_id) if action.contact_id else nullcontext()
    try:
        with transaction.atomic(), lock:
            handler(action.payload, action)
            _mark_done(action)
    except Exception as exc:  # noqa: BLE001 - a handler must not be able to kill the worker
        elapsed_ms = int((time.monotonic() - started) * 1000)
        status = _record_failure(action, _error_text(exc), permanent=False)
        logger.exception(
            "Queue action failed id=%s type=%s attempts=%s/%s next_status=%s duration_ms=%s",
            action.pk,
            action.type,
            action.attempts,
            action.max_attempts,
            status,
            elapsed_ms,
        )
        return status

    elapsed_ms = int((time.monotonic() - started) * 1000)
    logger.info(
        "Queue action done id=%s type=%s workspace=%s attempts=%s duration_ms=%s",
        action.pk,
        action.type,
        action.workspace_id or "system",
        action.attempts,
        elapsed_ms,
    )
    return str(ActionStatus.DONE)


def run_batch(batch_size: int = DEFAULT_BATCH_SIZE) -> BatchResult:
    """Claim one batch and process every row in it."""
    batch_size = positive_int(batch_size)
    actions = claim_batch(batch_size)
    result = BatchResult(claimed=len(actions))
    for action in actions:
        try:
            status = process_action(action)
        except Exception:  # noqa: BLE001 - a row that cannot even record its own failure
            # Reached only when the failure bookkeeping itself raised (a dropped
            # connection, say). Leave the row 'running' and let zombie recovery
            # pick it up rather than abandoning the rest of the batch.
            logger.exception("Queue action %s could not be finalised; leaving it to zombie recovery", action.pk)
            result.stranded += 1
            continue
        if status == ActionStatus.DONE:
            result.done += 1
        elif status == ActionStatus.FAILED:
            result.failed += 1
        else:
            result.retried += 1
    return result


def drain(
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_seconds: float | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> BatchResult:
    """Claim and process until the queue is empty, the budget runs out, or asked to stop.

    ``max_seconds`` is checked between batches, not inside one: a batch that has
    been claimed is already ``running`` in the database, and abandoning it would
    hand the rows to zombie recovery ten minutes later instead of finishing work
    that is a few milliseconds from done.
    """
    batch_size = positive_int(batch_size)
    total = BatchResult()
    deadline = None if max_seconds is None else time.monotonic() + max_seconds
    while True:
        if should_stop is not None and should_stop():
            break
        result = run_batch(batch_size)
        total += result
        if result.claimed < batch_size:
            # A short batch means the queue came up empty.
            break
        if deadline is not None and time.monotonic() >= deadline:
            break
    return total
