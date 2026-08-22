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

        assert reset_zombie_actions().startswith("reset 1 zombie action(s)")

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

        assert reset_zombie_actions().startswith("reset 3 zombie action(s)")

    def test_an_abandoned_action_out_of_attempts_becomes_terminal(self, tenancy: Tenancy) -> None:
        """A row that kills its worker never reaches _record_failure.

        The claim is the only thing that counts its attempts, so resetting it
        unconditionally meant claim, die, reset, claim, die — forever, with
        max_attempts never applying to the failure mode that most needs it.
        """
        poison = make_action(tenancy.workspace, status=ActionStatus.RUNNING, attempts=5, max_attempts=5)
        _stale(poison, timedelta(hours=1))

        summary = reset_zombie_actions()

        poison.refresh_from_db()
        assert poison.status == ActionStatus.FAILED
        assert "out of attempts" in poison.last_error
        assert "failed 1 out of attempts" in summary

    def test_an_abandoned_action_with_budget_left_still_comes_back(self, tenancy: Tenancy) -> None:
        survivor = make_action(tenancy.workspace, status=ActionStatus.RUNNING, attempts=2, max_attempts=5)
        _stale(survivor, timedelta(hours=1))

        reset_zombie_actions()

        survivor.refresh_from_db()
        assert survivor.status == ActionStatus.PENDING

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

    def test_one_failing_job_does_not_starve_the_others(self, monkeypatch: Any) -> None:
        # OPTIONAL_JOB_PATHS is emptied for the same reason only_housekeeping_jobs
        # empties the registry: this test asserts "which jobs failed", and a real
        # dotted-path job resolving alongside the two below would join the answer.
        # It is no longer hypothetical — L3-B (#9) landed
        # apps.flows.housekeeping.expire_stale_executions at the path this tuple
        # already reserved for it.
        monkeypatch.setattr(housekeeping, "OPTIONAL_JOB_PATHS", ())
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

    def test_a_broken_optional_module_cannot_stop_the_sweep(self, monkeypatch: Any) -> None:
        """Resolution runs outside run_housekeeping_jobs' per-job guard.

        An optional module that imports but raises would otherwise abort the
        whole sweep from outside that guard — taking zombie recovery, the one
        job that makes the queue self-healing, down with it.
        """
        monkeypatch.setattr(
            housekeeping,
            "OPTIONAL_JOB_PATHS",
            (("explodes", "apps.queueing.tests.exploding_probe.job"),),
        )
        ran: list[str] = []

        def good() -> str:
            ran.append("good")
            return "ok"

        with only_housekeeping_jobs(good=good):
            failures = run_housekeeping_jobs()

        assert ran == ["good"]
        assert failures == []

    def test_a_path_whose_attribute_is_missing_is_reported(self, monkeypatch: Any, caplog: Any) -> None:
        """Silence would be indistinguishable from "the app has not landed"."""
        monkeypatch.setattr(
            housekeeping,
            "OPTIONAL_JOB_PATHS",
            (("typo", "apps.queueing.tests.test_housekeeping.no_such_attribute"),),
        )
        with only_housekeeping_jobs(), caplog.at_level("WARNING", logger="apps.queueing.housekeeping"):
            jobs = housekeeping_jobs()

        assert jobs == {}
        assert "no callable" in caplog.text

    def test_a_module_whose_own_import_fails_is_reported_as_broken(self, monkeypatch: Any, caplog: Any) -> None:
        """import_module raises ImportError for "absent" AND for "present but
        its own import failed". Filing the second under "not landed yet" hid a
        broken installation at debug level and memoised it off for the process.
        """
        monkeypatch.setattr(
            housekeeping,
            "OPTIONAL_JOB_PATHS",
            (("broken", "apps.queueing.tests.broken_import_probe.job"),),
        )
        with only_housekeeping_jobs(), caplog.at_level("ERROR", logger="apps.queueing.housekeeping"):
            jobs = housekeeping_jobs()

        assert jobs == {}
        assert "could not be imported" in caplog.text

    def test_an_absent_module_is_only_looked_up_once(self, monkeypatch: Any) -> None:
        """Python does not cache failed imports, so an unmemoised bridge would
        redo a full sys.path search on every sweep."""
        attempts: list[str] = []

        def counting_find_spec(name: str) -> Any:
            attempts.append(name)
            return None

        monkeypatch.setattr(housekeeping, "OPTIONAL_JOB_PATHS", (("absent", "apps.nope.module.job"),))
        # find_spec is what decides "absent" now; import_module is never
        # reached for a module that is not there.
        monkeypatch.setattr(housekeeping.importlib.util, "find_spec", counting_find_spec)

        with only_housekeeping_jobs():
            housekeeping_jobs()
            housekeeping_jobs()
            housekeeping_jobs()

        assert attempts == ["apps.nope.module"]

    def test_dotted_path_jobs_for_apps_that_have_not_landed_are_skipped(self, monkeypatch: Any) -> None:
        """A path whose module is absent resolves to nothing, quietly.

        Written against a synthetic path rather than the real tuple, which no
        longer answers the question: L3-B (#9) has landed
        ``apps.flows.housekeeping.expire_stale_executions``, so the real tuple
        now resolves one job and asserting an empty registry would be asserting
        that a shipped feature is missing.
        """
        monkeypatch.setattr(housekeeping, "OPTIONAL_JOB_PATHS", (("absent", "apps.nonexistent.module.job"),))
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

    def test_a_tenant_row_named_housekeeping_does_not_suppress_the_chain(self, tenancy: Tenancy) -> None:
        """The liveness check asks about the *system* chain, not the type name.

        Without the workspace__isnull filter, any row merely typed
        "housekeeping" satisfied it — so a fixture or a later layer reusing the
        name would leave every worker reporting a healthy chain while zombie
        recovery never ran.
        """
        decoy = make_action(tenancy.workspace, type=ActionType.HOUSEKEEPING, status=ActionStatus.PENDING)

        chain = ensure_housekeeping_scheduled()

        assert chain.pk != decoy.pk
        assert chain.workspace_id is None

    def test_a_stale_running_sweep_does_not_suppress_the_chain_forever(self) -> None:
        """The deadlock: a worker dies holding the sweep.

        The row stays `running` and nothing can move it — the claim only looks
        at pending, and the only thing that resets abandoned rows is zombie
        recovery, which is a job inside the sweep that is now stuck. Accepting
        a stale running row here stopped housekeeping permanently while every
        worker start reported it healthy.
        """
        stuck = ensure_housekeeping_scheduled()
        ScheduledAction.objects.unscoped().filter(pk=stuck.pk).update(
            status=ActionStatus.RUNNING, updated_at=timezone.now() - ZOMBIE_AFTER - timedelta(minutes=1)
        )

        revived = ensure_housekeeping_scheduled()

        assert revived.pk != stuck.pk
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

    def test_a_failing_job_does_not_roll_back_the_successor(self, tenancy: Tenancy) -> None:
        """The sweep runs inside process_action's transaction.

        Raising to "retry with backoff" rolled back everything the sweep had
        just done — the successor row and every job that had already
        succeeded, zombie recovery included. One permanently broken job ended
        all housekeeping.
        """
        zombie = make_action(tenancy.workspace, status=ActionStatus.RUNNING)
        _stale(zombie, timedelta(hours=1))
        sweep = ensure_housekeeping_scheduled()
        ScheduledAction.objects.unscoped().filter(pk=sweep.pk).update(run_at=timezone.now() - timedelta(seconds=1))

        def bad() -> str:
            raise RuntimeError("nope")

        # Through the real worker, so the handler runs inside the transaction
        # that made raising destructive.
        with only_housekeeping_jobs(bad=bad, zombies=reset_zombie_actions):
            claimed = next(row for row in claim_batch() if row.pk == sweep.pk)
            assert process_action(claimed) == ActionStatus.DONE

        # The successor survived...
        assert (
            ScheduledAction.objects.unscoped()
            .filter(type=ActionType.HOUSEKEEPING, status=ActionStatus.PENDING)
            .exclude(pk=sweep.pk)
            .exists()
        )
        # ...and so did the work the healthy job did alongside the broken one.
        zombie.refresh_from_db()
        assert zombie.status == ActionStatus.PENDING

    def test_a_failing_job_is_logged_loudly(self, caplog: Any) -> None:
        action = ensure_housekeeping_scheduled()

        def bad() -> str:
            raise RuntimeError("nope")

        with (
            only_housekeeping_jobs(bad=bad),
            caplog.at_level("ERROR", logger="apps.queueing.housekeeping"),
        ):
            handle_housekeeping({}, action)

        assert "bad" in caplog.text

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
