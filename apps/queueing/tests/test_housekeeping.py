"""Zombie recovery, the job registry, and the chain that must never break."""

from datetime import timedelta
from typing import Any

import pytest
from django.utils import timezone

from apps.queueing import housekeeping
from apps.queueing.housekeeping import (
    ZOMBIE_AFTER,
    ensure_housekeeping_scheduled,
    handle_housekeeping,
    housekeeping_jobs,
    register_housekeeping_job,
    reset_zombie_actions,
    run_housekeeping_jobs,
)
from apps.queueing.models import ActionStatus, ActionType, ScheduledAction
from apps.queueing.tests.support import make_action, only_housekeeping_jobs
from apps.queueing.worker import claim_batch, process_action
from tests.support import Tenancy


def _stale(action: ScheduledAction, age: timedelta) -> None:
    """Rewind updated_at. auto_now makes save() useless for this, so update()."""
    ScheduledAction.objects.unscoped().filter(pk=action.pk).update(updated_at=timezone.now() - age)


@pytest.mark.django_db
class TestZombieRecovery:
    def test_an_abandoned_running_row_comes_back_to_pending(self, tenancy: Tenancy) -> None:
        """Nothing else ever looks at a running row; this is the only recovery path."""
        action = make_action(tenancy.workspace, status=ActionStatus.RUNNING, attempts=1)
        _stale(action, ZOMBIE_AFTER + timedelta(minutes=1))

        assert reset_zombie_actions() == "reset 1 zombie action(s)"

        action.refresh_from_db()
        assert action.status == ActionStatus.PENDING
        assert action.attempts == 1  # the crashed attempt still counted

    def test_a_row_a_worker_is_still_holding_is_left_alone(self, tenancy: Tenancy) -> None:
        action = make_action(tenancy.workspace, status=ActionStatus.RUNNING)
        _stale(action, ZOMBIE_AFTER - timedelta(minutes=1))

        reset_zombie_actions()

        action.refresh_from_db()
        assert action.status == ActionStatus.RUNNING

    def test_terminal_rows_are_never_resurrected(self, tenancy: Tenancy) -> None:
        for status in (ActionStatus.DONE, ActionStatus.FAILED, ActionStatus.CANCELLED, ActionStatus.PENDING):
            action = make_action(tenancy.workspace, status=status)
            _stale(action, timedelta(days=1))

        reset_zombie_actions()

        assert not ScheduledAction.objects.unscoped().filter(status=ActionStatus.RUNNING).exists()
        assert ScheduledAction.objects.unscoped().filter(status=ActionStatus.DONE).count() == 1

    def test_it_sweeps_every_tenant_and_system_rows_too(self, tenancy: Tenancy, other_tenancy: Tenancy) -> None:
        for workspace in (tenancy.workspace, other_tenancy.workspace, None):
            _stale(make_action(workspace, status=ActionStatus.RUNNING), timedelta(hours=1))

        assert reset_zombie_actions() == "reset 3 zombie action(s)"

    def test_the_clock_can_be_driven_from_the_call_site(self, tenancy: Tenancy) -> None:
        """Lets the sweep be tested at an arbitrary moment without freezing time."""
        action = make_action(tenancy.workspace, status=ActionStatus.RUNNING)
        _stale(action, timedelta(minutes=5))

        reset_zombie_actions(now=timezone.now() + timedelta(minutes=10))

        action.refresh_from_db()
        assert action.status == ActionStatus.PENDING


class TestJobRegistry:
    def test_zombie_recovery_is_registered_out_of_the_box(self) -> None:
        assert "reset_zombie_actions" in housekeeping_jobs()

    def test_a_duplicate_name_raises(self) -> None:
        with only_housekeeping_jobs(dupe=lambda: None), pytest.raises(RuntimeError, match="already registered"):
            register_housekeeping_job("dupe")(lambda: None)

    def test_one_failing_job_does_not_starve_the_others(self) -> None:
        ran: list[str] = []

        def ok() -> str:
            ran.append("ok")
            return "fine"

        def bad() -> str:
            ran.append("bad")
            raise RuntimeError("nope")

        # Alphabetical insert order puts the failure first, which is the case
        # that a naive loop would get wrong.
        with only_housekeeping_jobs(bad=bad, ok=ok):
            failures = run_housekeeping_jobs()

        assert ran == ["bad", "ok"]
        assert failures == ["bad"]

    def test_dotted_path_jobs_for_apps_that_have_not_landed_are_skipped(self) -> None:
        """L2-B, L3-B and L5-C all register into this; none of them exist yet."""
        with only_housekeeping_jobs():
            assert housekeeping_jobs() == {}

    def test_a_dotted_path_job_is_picked_up_when_its_module_imports(self, monkeypatch: Any) -> None:
        """And this is the shape L2-B's prune will arrive in."""
        monkeypatch.setattr(
            housekeeping,
            "OPTIONAL_JOB_PATHS",
            (
                ("prune_probe", "apps.queueing.tests.test_housekeeping._importable_job"),
                ("absent_probe", "apps.nonexistent.module.some_job"),
            ),
        )

        with only_housekeeping_jobs():
            jobs = housekeeping_jobs()

        assert jobs == {"prune_probe": _importable_job}


@pytest.mark.django_db
class TestTheChain:
    def test_bootstrapping_creates_exactly_one_pending_sweep(self) -> None:
        first = ensure_housekeeping_scheduled()
        second = ensure_housekeeping_scheduled()

        assert second.pk == first.pk
        assert first.workspace_id is None  # deployment-level, not a tenant's
        assert ScheduledAction.objects.unscoped().filter(type=ActionType.HOUSEKEEPING).count() == 1

    def test_bootstrapping_repairs_a_chain_that_died(self) -> None:
        """A migration could not do this; a worker start can, every time it starts."""
        dead = ensure_housekeeping_scheduled()
        ScheduledAction.objects.unscoped().filter(pk=dead.pk).update(status=ActionStatus.FAILED)

        revived = ensure_housekeeping_scheduled()

        assert revived.pk != dead.pk
        assert revived.status == ActionStatus.PENDING

    def test_bootstrapping_leaves_a_running_sweep_alone(self) -> None:
        running = ensure_housekeeping_scheduled()
        ScheduledAction.objects.unscoped().filter(pk=running.pk).update(status=ActionStatus.RUNNING)

        assert ensure_housekeeping_scheduled().pk == running.pk

    def test_a_sweep_schedules_its_successor(self) -> None:
        action = ensure_housekeeping_scheduled()
        with only_housekeeping_jobs(noop=lambda: "fine"):
            handle_housekeeping({}, action)

        successor = (
            ScheduledAction.objects.unscoped()
            .filter(type=ActionType.HOUSEKEEPING, status=ActionStatus.PENDING)
            .exclude(pk=action.pk)
            .get()
        )
        assert timedelta(minutes=59) <= successor.run_at - timezone.now() <= timedelta(minutes=61)

    def test_a_sweep_that_throws_still_leaves_a_successor(self) -> None:
        """Otherwise one bad hour ends housekeeping forever — including zombie recovery."""
        action = ensure_housekeeping_scheduled()

        def bad() -> str:
            raise RuntimeError("nope")

        with only_housekeeping_jobs(bad=bad), pytest.raises(RuntimeError, match="Housekeeping jobs failed"):
            handle_housekeeping({}, action)

        assert (
            ScheduledAction.objects.unscoped()
            .filter(type=ActionType.HOUSEKEEPING, status=ActionStatus.PENDING)
            .exclude(pk=action.pk)
            .exists()
        )

    def test_two_sweeps_in_one_hour_converge_on_one_successor(self) -> None:
        """Per-hour idempotency keys: two workers and a tick cannot fan the chain out."""
        first = ensure_housekeeping_scheduled()
        with only_housekeeping_jobs(noop=lambda: None):
            handle_housekeeping({}, first)
            handle_housekeeping({}, first)

        assert ScheduledAction.objects.unscoped().filter(type=ActionType.HOUSEKEEPING).count() == 2

    def test_the_sweep_runs_end_to_end_through_the_worker(self, tenancy: Tenancy) -> None:
        """The real path: a claimed housekeeping row resets a zombie and completes."""
        zombie = make_action(tenancy.workspace, status=ActionStatus.RUNNING)
        _stale(zombie, timedelta(hours=1))
        sweep = ensure_housekeeping_scheduled()
        # The claim compares run_at against Postgres' now(), which is the
        # *transaction* start time — and this test runs inside one. In
        # production the claim is its own autocommit statement, so a row
        # scheduled for "now" is due immediately; here it needs a nudge.
        ScheduledAction.objects.unscoped().filter(pk=sweep.pk).update(run_at=timezone.now() - timedelta(seconds=1))

        claimed = next(row for row in claim_batch() if row.pk == sweep.pk)
        assert process_action(claimed) == ActionStatus.DONE

        zombie.refresh_from_db()
        assert zombie.status == ActionStatus.PENDING


def _importable_job() -> str:
    """Stands in for a sibling app's housekeeping job, resolved by dotted path."""
    return "pruned"
