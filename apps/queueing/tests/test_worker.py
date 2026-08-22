"""Claiming, running, retrying and failing (SPEC §15, §9.5)."""

import time
import uuid
from datetime import timedelta
from typing import Any

import pytest
from django.db import connection
from django.utils import timezone

from apps.common.models import RateLimitCounter
from apps.queueing.locks import contact_lock_key
from apps.queueing.models import ActionStatus, ScheduledAction
from apps.queueing.tests.support import make_action, temporary_handler
from apps.queueing.worker import (
    BACKOFF_SCHEDULE,
    BatchResult,
    claim_batch,
    drain,
    next_run_at,
    process_action,
    run_batch,
)
from tests.support import Tenancy

PROBE = "worker_probe"


def _noop(payload: dict[str, Any], action: ScheduledAction) -> None:
    return None


def _boom(payload: dict[str, Any], action: ScheduledAction) -> None:
    raise RuntimeError("handler exploded")


class TestBackoffSchedule:
    def test_matches_the_spec_exactly(self) -> None:
        """SPEC §9.5 / §15: 30s, 2m, 10m, 1h, 6h."""
        assert BACKOFF_SCHEDULE == (30, 120, 600, 3600, 21600)

    @pytest.mark.parametrize(
        ("attempts", "expected_seconds"),
        [(1, 30), (2, 120), (3, 600), (4, 3600), (5, 21600)],
    )
    def test_each_attempt_gets_its_delay(self, attempts: int, expected_seconds: int) -> None:
        now = timezone.now()
        assert next_run_at(attempts, now) == now + timedelta(seconds=expected_seconds)

    def test_beyond_the_schedule_it_clamps_rather_than_falling_off_the_end(self) -> None:
        """A raised max_attempts keeps retrying every 6h, not every 0s."""
        now = timezone.now()
        assert next_run_at(99, now) == now + timedelta(seconds=21600)

    def test_a_zero_attempt_count_is_treated_as_the_first(self) -> None:
        now = timezone.now()
        assert next_run_at(0, now) == now + timedelta(seconds=30)


@pytest.mark.django_db
class TestClaim:
    def test_claims_only_rows_that_are_due(self, tenancy: Tenancy) -> None:
        due = make_action(tenancy.workspace)
        make_action(tenancy.workspace, run_at=timezone.now() + timedelta(hours=1))

        claimed = claim_batch()

        assert [row.pk for row in claimed] == [due.pk]

    def test_claims_only_pending_rows(self, tenancy: Tenancy) -> None:
        for status in (ActionStatus.RUNNING, ActionStatus.DONE, ActionStatus.FAILED, ActionStatus.CANCELLED):
            make_action(tenancy.workspace, status=status)
        pending = make_action(tenancy.workspace)

        assert [row.pk for row in claim_batch()] == [pending.pk]

    def test_marks_them_running_and_increments_attempts(self, tenancy: Tenancy) -> None:
        action = make_action(tenancy.workspace)

        claimed = claim_batch()[0]

        assert claimed.status == ActionStatus.RUNNING
        assert claimed.attempts == 1
        action.refresh_from_db()
        assert action.status == ActionStatus.RUNNING

    def test_sets_updated_at_so_a_healthy_row_is_not_mistaken_for_a_zombie(self, tenancy: Tenancy) -> None:
        """Raw SQL does not get Django's auto_now; zombie recovery reads this column."""
        stale = timezone.now() - timedelta(hours=2)
        action = make_action(tenancy.workspace)
        ScheduledAction.objects.unscoped().filter(pk=action.pk).update(updated_at=stale)

        claimed = claim_batch()[0]

        assert claimed.updated_at > stale

    def test_respects_the_batch_limit_and_takes_the_oldest_first(self, tenancy: Tenancy) -> None:
        now = timezone.now()
        oldest = [make_action(tenancy.workspace, run_at=now - timedelta(minutes=10 - i)) for i in range(5)]

        claimed = claim_batch(2)

        assert [row.pk for row in claimed] == [oldest[0].pk, oldest[1].pk]

    def test_a_second_claim_sees_nothing(self, tenancy: Tenancy) -> None:
        """The first claim committed 'running', so there is nothing left to take."""
        make_action(tenancy.workspace)
        assert len(claim_batch()) == 1
        assert claim_batch() == []

    def test_the_claim_crosses_tenants_on_purpose(self, tenancy: Tenancy, other_tenancy: Tenancy) -> None:
        """One worker drains the whole deployment, tenant and system rows alike."""
        mine = make_action(tenancy.workspace)
        theirs = make_action(other_tenancy.workspace)
        system = make_action(None, type="housekeeping")

        claimed = {row.pk for row in claim_batch()}

        assert claimed == {mine.pk, theirs.pk, system.pk}

    @pytest.mark.parametrize("limit", [0, -1])
    def test_a_non_positive_limit_is_refused(self, tenancy: Tenancy, limit: int) -> None:
        """It used to return [], which is what made drain() spin forever."""
        make_action(tenancy.workspace)
        with pytest.raises(ValueError, match="1 or greater"):
            claim_batch(limit)


@pytest.mark.django_db
class TestProcessAction:
    def test_a_successful_handler_marks_the_row_done(self, tenancy: Tenancy) -> None:
        make_action(tenancy.workspace, type=PROBE)
        with temporary_handler(PROBE, _noop):
            action = claim_batch()[0]
            assert process_action(action) == ActionStatus.DONE

        action.refresh_from_db()
        assert action.status == ActionStatus.DONE
        assert action.last_error == ""

    def test_the_handler_receives_the_payload_and_the_row(self, tenancy: Tenancy) -> None:
        seen: dict[str, Any] = {}

        def capture(payload: dict[str, Any], action: ScheduledAction) -> None:
            seen["payload"] = payload
            seen["pk"] = action.pk

        created = make_action(tenancy.workspace, type=PROBE, payload={"contact": "x"})
        with temporary_handler(PROBE, capture):
            process_action(claim_batch()[0])

        assert seen == {"payload": {"contact": "x"}, "pk": created.pk}

    def test_a_failure_reschedules_with_the_spec_backoff(self, tenancy: Tenancy) -> None:
        make_action(tenancy.workspace, type=PROBE)
        with temporary_handler(PROBE, _boom):
            before = timezone.now()
            action = claim_batch()[0]
            assert process_action(action) == ActionStatus.PENDING

        action.refresh_from_db()
        assert action.status == ActionStatus.PENDING
        assert action.attempts == 1
        # First failure: 30 seconds out, give or take the test's own runtime.
        assert timedelta(seconds=29) <= action.run_at - before <= timedelta(seconds=31)
        assert "handler exploded" in action.last_error

    def test_a_failing_handlers_writes_are_rolled_back(self, tenancy: Tenancy) -> None:
        """The handler's transaction is the row's transaction: half-done work cannot commit."""

        def write_then_fail(payload: dict[str, Any], action: ScheduledAction) -> None:
            RateLimitCounter.objects.create(key="half-done", count=1, expires_at=timezone.now())
            raise RuntimeError("too late")

        make_action(tenancy.workspace, type=PROBE)
        with temporary_handler(PROBE, write_then_fail):
            process_action(claim_batch()[0])

        assert not RateLimitCounter.objects.filter(key="half-done").exists()

    def test_the_row_still_records_its_failure_after_that_rollback(self, tenancy: Tenancy) -> None:
        """The bookkeeping runs in a *fresh* transaction, or it would roll back too."""
        make_action(tenancy.workspace, type=PROBE)
        with temporary_handler(PROBE, _boom):
            action = claim_batch()[0]
            process_action(action)

        action.refresh_from_db()
        assert action.last_error.startswith("RuntimeError:")

    def test_the_last_attempt_fails_terminally(self, tenancy: Tenancy) -> None:
        make_action(tenancy.workspace, type=PROBE, attempts=4, max_attempts=5)
        with temporary_handler(PROBE, _boom):
            action = claim_batch()[0]
            assert process_action(action) == ActionStatus.FAILED

        action.refresh_from_db()
        assert action.status == ActionStatus.FAILED
        assert action.attempts == 5

    def test_a_raised_budget_reaches_the_six_hour_step(self, tenancy: Tenancy) -> None:
        """With the default max_attempts=5 the 6h delay is never used; document it."""
        make_action(tenancy.workspace, type=PROBE, attempts=4, max_attempts=6)
        with temporary_handler(PROBE, _boom):
            before = timezone.now()
            action = claim_batch()[0]
            assert process_action(action) == ActionStatus.PENDING

        action.refresh_from_db()
        assert timedelta(hours=5, minutes=59) <= action.run_at - before <= timedelta(hours=6, minutes=1)

    def test_an_unknown_type_fails_immediately_without_burning_retries(self, tenancy: Tenancy) -> None:
        """Retrying a type nothing handles cannot succeed; five tries over six hours is noise."""
        make_action(tenancy.workspace, type="nobody_handles_this")
        action = claim_batch()[0]

        assert process_action(action) == ActionStatus.FAILED

        action.refresh_from_db()
        assert action.status == ActionStatus.FAILED
        assert action.attempts == 1
        assert "No handler registered" in action.last_error

    def test_every_stored_error_is_scrubbed_whichever_path_wrote_it(self, tenancy: Tenancy, secret_value: str) -> None:
        """Scrubbing lives in _record_failure, so both writers are covered."""
        from apps.queueing.worker import _record_failure

        action = make_action(tenancy.workspace, type=PROBE)
        _record_failure(action, f"token={secret_value}", permanent=True)

        action.refresh_from_db()
        assert secret_value not in action.last_error
        assert "[REDACTED]" in action.last_error

    def test_a_stored_error_is_scrubbed(self, tenancy: Tenancy, secret_value: str) -> None:
        """last_error is a plain column rendered in the admin (SECURITY-BASELINE §5)."""

        def leak(payload: dict[str, Any], action: ScheduledAction) -> None:
            raise RuntimeError(f"POST failed: token={secret_value}")

        make_action(tenancy.workspace, type=PROBE)
        with temporary_handler(PROBE, leak):
            action = claim_batch()[0]
            process_action(action)

        action.refresh_from_db()
        assert secret_value not in action.last_error
        assert "[REDACTED]" in action.last_error

    def test_a_stored_error_is_capped(self, tenancy: Tenancy) -> None:
        def flood(payload: dict[str, Any], action: ScheduledAction) -> None:
            raise RuntimeError("x" * 50_000)

        make_action(tenancy.workspace, type=PROBE)
        with temporary_handler(PROBE, flood):
            action = claim_batch()[0]
            process_action(action)

        action.refresh_from_db()
        assert len(action.last_error) <= 2000

    def test_the_contact_lock_is_held_while_the_handler_runs(self, tenancy: Tenancy) -> None:
        """SPEC §9.6: claim, then take the contact lock, then touch the execution."""
        contact_id = uuid.uuid4()
        observed: dict[str, bool] = {}

        def check_lock(payload: dict[str, Any], action: ScheduledAction) -> None:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                    "AND objid = (SELECT hashtext(%s)::bigint & 4294967295) AND granted",
                    [contact_lock_key(contact_id)],
                )
                row = cursor.fetchone()
            observed["held"] = bool(row and row[0])

        make_action(tenancy.workspace, type=PROBE, contact_id=contact_id)
        with temporary_handler(PROBE, check_lock):
            process_action(claim_batch()[0])

        assert observed["held"] is True

    def test_a_row_with_no_contact_takes_no_lock(self, tenancy: Tenancy) -> None:
        """Broadcast fanout and housekeeping would otherwise serialise on nothing."""
        observed: dict[str, int] = {}

        def count_locks(payload: dict[str, Any], action: ScheduledAction) -> None:
            with connection.cursor() as cursor:
                cursor.execute("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'")
                observed["locks"] = cursor.fetchone()[0]

        make_action(tenancy.workspace, type=PROBE)
        with temporary_handler(PROBE, count_locks):
            process_action(claim_batch()[0])

        assert observed["locks"] == 0


@pytest.mark.django_db
class TestBatches:
    def test_run_batch_counts_each_outcome(self, tenancy: Tenancy) -> None:
        make_action(tenancy.workspace, type=PROBE)
        make_action(tenancy.workspace, type="nobody_handles_this")
        make_action(tenancy.workspace, type="also_unhandled")

        with temporary_handler(PROBE, _noop):
            result = run_batch()

        assert (result.claimed, result.done, result.failed, result.retried) == (3, 1, 2, 0)

    def test_drain_keeps_going_until_the_queue_is_empty(self, tenancy: Tenancy) -> None:
        for _ in range(7):
            make_action(tenancy.workspace, type=PROBE)

        with temporary_handler(PROBE, _noop):
            result = drain(batch_size=2)

        assert result.claimed == 7
        assert result.done == 7
        assert not ScheduledAction.objects.for_workspace(tenancy.workspace).exclude(status=ActionStatus.DONE).exists()

    def test_drain_stops_when_asked(self, tenancy: Tenancy) -> None:
        for _ in range(10):
            make_action(tenancy.workspace, type=PROBE)

        with temporary_handler(PROBE, _noop):
            result = drain(batch_size=2, should_stop=lambda: True)

        assert result.claimed == 0

    def test_each_action_stamps_updated_at_before_it_runs(self, tenancy: Tenancy) -> None:
        """The claim stamps the whole batch at once and actions run serially.

        Without a per-action stamp, the last row of a slow batch still carries
        the batch's claim time, so another worker's zombie sweep returns live
        work to pending and runs it a second time. Contact locks only serialise
        that duplicate; rows with no contact are not locked at all.
        """
        seen: dict[str, Any] = {}
        make_action(tenancy.workspace, type=PROBE)
        claimed = claim_batch()[0]
        # Backdate the claim stamp to what a long batch would leave behind.
        stale = timezone.now() - timedelta(hours=1)
        ScheduledAction.objects.unscoped().filter(pk=claimed.pk).update(updated_at=stale)

        def observe(payload: dict[str, Any], action: ScheduledAction) -> None:
            seen["updated_at"] = ScheduledAction.objects.unscoped().get(pk=action.pk).updated_at

        with temporary_handler(PROBE, observe):
            process_action(claimed)

        assert seen["updated_at"] > stale, "the row still looked abandoned while it was running"

    def test_a_drain_out_of_budget_releases_unstarted_rows(self, tenancy: Tenancy) -> None:
        """They were claimed but never handed to a handler.

        Leaving them `running` stranded them until zombie recovery ten minutes
        later; the HTTP tick has a real budget (gunicorn kills the request), so
        this is the difference between a 502 with stuck rows and a clean hand-back.
        """
        for _ in range(5):
            make_action(tenancy.workspace, type=PROBE)

        def slow(payload: dict[str, Any], action: ScheduledAction) -> None:
            time.sleep(0.05)

        with temporary_handler(PROBE, slow):
            # A budget that expires during the batch, not between batches.
            result = run_batch(5, deadline=time.monotonic() + 0.06)

        pending = ScheduledAction.objects.for_workspace(tenancy.workspace).filter(status=ActionStatus.PENDING)
        assert pending.exists(), "unstarted rows should be back in the queue, not left running"
        assert not ScheduledAction.objects.unscoped().filter(status=ActionStatus.RUNNING).exists()
        # Released rows give their attempt back: nobody ran them.
        assert all(action.attempts == 0 for action in pending)
        assert result.claimed == result.done

    def test_batch_result_adds_up(self) -> None:
        total = BatchResult()
        total += BatchResult(claimed=2, done=1, failed=1)
        total += BatchResult(claimed=3, done=2, stranded=1)
        assert (total.claimed, total.done, total.failed, total.stranded) == (5, 3, 1, 1)

    @pytest.mark.parametrize("batch_size", [0, -1])
    def test_drain_refuses_a_non_positive_batch_size(self, batch_size: int) -> None:
        """The termination test is `claimed < batch_size`, and 0 < 0 is False.

        Before the guard, drain(batch_size=0) claimed nothing, never satisfied
        its own empty-queue check, and spun at full CPU forever.
        """
        with pytest.raises(ValueError, match="1 or greater"):
            drain(batch_size=batch_size)

    @pytest.mark.parametrize("batch_size", [0, -1])
    def test_run_batch_refuses_a_non_positive_batch_size(self, batch_size: int) -> None:
        with pytest.raises(ValueError, match="1 or greater"):
            run_batch(batch_size)

    def test_a_row_that_cannot_record_its_failure_is_counted_as_stranded(
        self, tenancy: Tenancy, monkeypatch: Any
    ) -> None:
        """It stays `running` for zombie recovery, so calling it `failed` would
        send an operator looking for a terminal row that is not there."""
        make_action(tenancy.workspace, type=PROBE)

        def explode(*args: Any, **kwargs: Any) -> str:
            raise RuntimeError("connection dropped while recording the failure")

        from apps.queueing import worker as worker_module

        with temporary_handler(PROBE, _boom):
            # Patch the module object rather than a dotted path: the string form
            # re-resolves through sys.modules, which other apps' tests stub.
            monkeypatch.setattr(worker_module, "_record_failure", explode)
            result = run_batch()

        assert (result.claimed, result.stranded, result.failed) == (1, 1, 0)
        assert ScheduledAction.objects.unscoped().filter(status=ActionStatus.RUNNING).count() == 1
