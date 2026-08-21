"""The acceptance criterion: two workers and a tick over 1 000 due actions.

    Two workers + tick running concurrently over 1k due actions: every action
    processed exactly once (assert via handler-side counter), including forced
    crashes mid-batch (zombie recovery test with clock manipulation).

Nothing here is mocked. Real threads, real connections, the real claim
statement. What makes it safe is ``FOR UPDATE SKIP LOCKED``: concurrent claims
select disjoint row sets, so "exactly once" is a property of the statement
rather than of the workers agreeing to take turns.

``transaction=True`` because that is what gives each thread a connection that
can commit — the ordinary ``django_db`` fixture wraps everything in one
transaction that no other thread can see into.
"""

import threading
from datetime import timedelta
from typing import Any

import pytest
from django.db import connections
from django.utils import timezone

from apps.queueing.housekeeping import ZOMBIE_AFTER, reset_zombie_actions
from apps.queueing.models import ActionStatus, ScheduledAction
from apps.queueing.tests.support import temporary_handler
from apps.queueing.worker import claim_batch, drain
from tests.support import create_tenancy
from tests.testapp.models import QueueProbe

PROBE = "concurrency_probe"
ACTION_COUNT = 1000
WORKER_COUNT = 2


def _run_drain() -> None:
    """One worker or tick process, in a thread with its own connection."""
    try:
        while drain(batch_size=50).claimed:
            pass
    finally:
        connections.close_all()


def _drain_concurrently(names: list[str], timeout: float = 120.0) -> None:
    """Start one drain thread per name and wait for all of them."""
    threads = [threading.Thread(target=_run_drain, name=name) for name in names]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=timeout)
        assert not thread.is_alive(), f"{thread.name} did not finish within {timeout}s"


@pytest.mark.django_db(transaction=True)
class TestExactlyOnce:
    def test_two_workers_and_a_tick_process_every_action_exactly_once(self) -> None:
        tenancy = create_tenancy("concurrency")
        due = timezone.now() - timedelta(seconds=1)
        ScheduledAction.objects.bulk_create(
            ScheduledAction(workspace=tenancy.workspace, run_at=due, type=PROBE, payload={"n": n})
            for n in range(ACTION_COUNT)
        )

        def record(payload: dict[str, Any], action: ScheduledAction) -> None:
            # Unique on action_id: a duplicate execution raises here, inside the
            # handler's transaction, instead of showing up as a bad total later.
            QueueProbe.objects.create(action_id=action.pk, worker=threading.current_thread().name)

        with temporary_handler(PROBE, record):
            _drain_concurrently(["worker-1", "worker-2", "tick"])

        assert QueueProbe.objects.count() == ACTION_COUNT
        assert QueueProbe.objects.values("action_id").distinct().count() == ACTION_COUNT
        assert ScheduledAction.objects.unscoped().filter(type=PROBE, status=ActionStatus.DONE).count() == ACTION_COUNT
        assert not ScheduledAction.objects.unscoped().filter(type=PROBE).exclude(status=ActionStatus.DONE).exists()

        # And prove the workers really did run concurrently — a test where one
        # thread happened to do everything would pass every assertion above
        # while proving nothing about contention.
        assert QueueProbe.objects.values("worker").distinct().count() > 1

    def test_a_crash_mid_batch_is_recovered_and_still_runs_exactly_once(self) -> None:
        """Forced crash mid-batch, then zombie recovery, with the clock driven by hand.

        The design collapses the crash window to a single point, which is what
        makes this testable without killing a process: the handler's writes and
        the row's ``done`` marking share one transaction, so a worker that dies
        can only ever die *between the claim committing and that transaction
        committing*. Claiming a batch and walking away is therefore not an
        approximation of a ``SIGKILL`` — it is the same state.

        Exactly-once holds across the recovery because the crash lands before
        any side effect. The queue's guarantee in general is at-least-once,
        which is why handlers are documented as needing to be idempotent.
        """
        tenancy = create_tenancy("crash")
        due = timezone.now() - timedelta(seconds=1)
        ScheduledAction.objects.bulk_create(
            ScheduledAction(workspace=tenancy.workspace, run_at=due, type=PROBE, payload={"n": n}) for n in range(200)
        )

        def record(payload: dict[str, Any], action: ScheduledAction) -> None:
            QueueProbe.objects.create(action_id=action.pk, worker=threading.current_thread().name)

        # The crash: a worker claims 50 rows and dies. The claim is committed,
        # so the rows say 'running' with nothing running them.
        killed = [action.pk for action in claim_batch(50)]
        assert len(killed) == 50

        with temporary_handler(PROBE, record):
            _drain_concurrently(["worker-1", "worker-2"])

        # Stranded, and no amount of draining will touch them: the claim
        # statement only ever looks at 'pending'.
        assert QueueProbe.objects.count() == 150
        assert ScheduledAction.objects.unscoped().filter(id__in=killed, status=ActionStatus.RUNNING).count() == 50
        with temporary_handler(PROBE, record):
            _drain_concurrently(["worker-1"])
        assert QueueProbe.objects.count() == 150

        # Clock manipulation: age the abandoned rows past the 10-minute cutoff,
        # which is what the hourly sweep would see ten minutes after the crash.
        ScheduledAction.objects.unscoped().filter(id__in=killed).update(
            updated_at=timezone.now() - ZOMBIE_AFTER - timedelta(minutes=1)
        )
        assert reset_zombie_actions().startswith("reset 50 zombie action(s)")

        with temporary_handler(PROBE, record):
            _drain_concurrently(["worker-1", "worker-2"])

        assert QueueProbe.objects.count() == 200
        assert QueueProbe.objects.values("action_id").distinct().count() == 200
        assert not ScheduledAction.objects.unscoped().filter(type=PROBE).exclude(status=ActionStatus.DONE).exists()

        # The crashed attempt still counted, so an action that crashes every
        # time runs out of budget instead of looping forever.
        recovered = ScheduledAction.objects.unscoped().filter(id__in=killed).first()
        assert recovered is not None
        assert recovered.attempts == 2
