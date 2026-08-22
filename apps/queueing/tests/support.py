"""Shared helpers for the queueing tests.

The handler registry is process-global, so every test that registers one has to
put it back — otherwise a test that asserts "unknown type fails" passes or fails
depending on which test ran first.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Any

from django.db import connection
from django.utils import timezone

from apps.queueing import housekeeping, registry
from apps.queueing.locks import contact_lock_key
from apps.queueing.models import ScheduledAction

# pg_locks is a CLUSTER-wide view. Advisory locks are acquired per database —
# the lock tag carries the database OID, so two databases cannot contend for the
# same key — but every session can still *see* every other database's advisory
# locks in pg_locks. Under `pytest -n auto` each xdist worker gets its own
# database on one shared server, so an unqualified `count(*) FROM pg_locks`
# counts the locks the other workers are holding right now.
#
# That is not hypothetical: it is exactly what an unfiltered count did to
# test_a_row_with_no_contact_takes_no_lock, which asserts the count is 0 while
# test_locks.py::TestContention deliberately holds a lock on another worker.
# Every query below is therefore scoped to the current database.


@contextmanager
def temporary_handler(action_type: str, func: registry.Handler) -> Iterator[registry.Handler]:
    """Register a handler for the duration of a test, then restore the registry."""
    previous = registry.get_handler(action_type)
    registry.register_handler(action_type, replace=True)(func)
    try:
        yield func
    finally:
        if previous is None:
            registry._HANDLERS.pop(action_type, None)
        else:
            registry._HANDLERS[action_type] = previous


@contextmanager
def only_housekeeping_jobs(**jobs: housekeeping.HousekeepingJob) -> Iterator[None]:
    """Run the sweep with exactly these jobs and nothing else.

    Replaces the whole registry rather than adding to it, so a test asserting
    "one job failed" is not also running zombie recovery against whatever rows
    the test happens to have created.
    """
    previous = dict(housekeeping._JOBS)
    # Resolution is memoised per process, so the attempted-paths set has to
    # reset with the registry or the second test to look at a dotted path finds
    # it already written off.
    previous_attempted = set(housekeeping._RESOLUTION_ATTEMPTED)
    housekeeping._JOBS.clear()
    housekeeping._JOBS.update(jobs)
    housekeeping._RESOLUTION_ATTEMPTED.clear()
    try:
        yield
    finally:
        housekeeping._JOBS.clear()
        housekeeping._JOBS.update(previous)
        housekeeping._RESOLUTION_ATTEMPTED.clear()
        housekeeping._RESOLUTION_ATTEMPTED.update(previous_attempted)


def make_action(workspace: Any = None, **overrides: Any) -> ScheduledAction:
    """A due, pending action. Straight through the model so tests can set anything."""
    fields: dict[str, Any] = {
        "workspace": workspace,
        "run_at": timezone.now() - timedelta(seconds=1),
        "type": "test_action",
        "payload": {},
    }
    fields.update(overrides)
    return ScheduledAction.objects.create(**fields)


def advisory_lock_count() -> int:
    """How many advisory locks this database holds. See the note above."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
            "AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
        )
        return int(cursor.fetchone()[0])


def contact_lock_is_held(contact_id: Any) -> bool:
    """Whether this database holds the contact lock, taken by any session.

    Deliberately not scoped to the current backend: the contention tests prove
    that a lock taken on one connection is visible from another.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
            "AND objid = (SELECT hashtext(%s)::bigint & 4294967295) "
            "AND database = (SELECT oid FROM pg_database WHERE datname = current_database()) "
            "AND granted",
            [contact_lock_key(contact_id)],
        )
        row = cursor.fetchone()
    return bool(row and row[0])
