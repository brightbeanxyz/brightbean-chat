"""``manage.py process_tasks`` and ``manage.py tick``."""

import signal
import threading
from datetime import timedelta
from io import StringIO
from typing import Any

import pytest
from django.core.management import call_command
from django.db import connections
from django.utils import timezone

from apps.queueing.models import ActionStatus, ScheduledAction
from apps.queueing.tests.support import make_action, temporary_handler
from tests.support import Tenancy

PROBE = "command_probe"


def _noop(payload: dict[str, Any], action: ScheduledAction) -> None:
    return None


@pytest.mark.django_db
class TestProcessTasks:
    def test_once_runs_one_batch_and_returns(self, tenancy: Tenancy) -> None:
        make_action(tenancy.workspace, type=PROBE)
        out = StringIO()

        with temporary_handler(PROBE, _noop):
            call_command("process_tasks", "--once", stdout=out)

        assert "1 done" in out.getvalue()
        assert ScheduledAction.objects.for_workspace(tenancy.workspace).filter(status=ActionStatus.DONE).count() == 1

    def test_max_batches_bounds_the_loop(self, tenancy: Tenancy) -> None:
        for _ in range(5):
            make_action(tenancy.workspace, type=PROBE)
        out = StringIO()

        with temporary_handler(PROBE, _noop):
            call_command("process_tasks", "--batch-size", "2", "--max-batches", "2", "--interval", "0", stdout=out)

        assert ScheduledAction.objects.for_workspace(tenancy.workspace).filter(status=ActionStatus.DONE).count() == 4

    @pytest.mark.parametrize("batch_size", ["0", "-1"])
    def test_a_non_positive_batch_size_is_refused(self, batch_size: str) -> None:
        """`claimed < batch_size` is False when both are 0, so the idle sleep
        never fired and the worker spun flat out claiming nothing."""
        from django.core.management import CommandError

        with pytest.raises((CommandError, SystemExit, ValueError)):
            call_command("process_tasks", "--once", "--batch-size", batch_size, stdout=StringIO())

    def test_it_bootstraps_the_housekeeping_chain_on_start(self) -> None:
        call_command("process_tasks", "--once", stdout=StringIO())
        assert ScheduledAction.objects.unscoped().filter(type="housekeeping").exists()

    def test_an_empty_queue_is_not_an_error(self) -> None:
        out = StringIO()
        call_command("process_tasks", "--once", stdout=out)
        assert "Processed" in out.getvalue()

    def test_sigterm_finishes_the_current_batch(self, tenancy: Tenancy) -> None:
        """Deploys and `docker stop` send this; a hard exit would cost 10 minutes.

        The signal is raised from *inside* a handler, which is the only moment
        that distinguishes "finish the batch" from "stop before the next one".
        """
        from apps.queueing.management.commands.process_tasks import Command

        command = Command()
        processed: list[Any] = []

        def handle_then_signal(payload: dict[str, Any], action: ScheduledAction) -> None:
            processed.append(action.pk)
            command._request_stop(signal.SIGTERM, None)

        first = make_action(tenancy.workspace, type=PROBE, run_at=timezone.now() - timedelta(minutes=2))
        second = make_action(tenancy.workspace, type=PROBE, run_at=timezone.now() - timedelta(minutes=1))
        out = StringIO()

        with temporary_handler(PROBE, handle_then_signal):
            command.stdout = out  # type: ignore[assignment]
            command.handle(batch_size=50, interval=0.0, once=False, max_batches=0)

        # Both rows were in the claimed batch, so both ran even though the
        # signal arrived during the first.
        assert processed == [first.pk, second.pk]
        for action in (first, second):
            action.refresh_from_db()
            assert action.status == ActionStatus.DONE


@pytest.mark.django_db(transaction=True)
class TestOffTheMainThread:
    """``transaction=True`` because the worker thread commits for real.

    A thread gets its own connection in autocommit, so anything it writes —
    the housekeeping bootstrap row, for one — outlives the wrapping transaction
    an ordinary ``django_db`` test rolls back, and leaks into whatever runs
    next.
    """

    def test_signal_handlers_off_the_main_thread_are_not_fatal(self) -> None:
        """A test harness or an embedded run is not the main thread."""
        errors: list[BaseException] = []

        def run() -> None:
            try:
                call_command("process_tasks", "--once", stdout=StringIO())
            except BaseException as exc:  # noqa: BLE001 - the point is that nothing escapes
                errors.append(exc)
            finally:
                connections.close_all()

        thread = threading.Thread(target=run)
        thread.start()
        thread.join(timeout=30)

        assert errors == []


@pytest.mark.django_db
class TestTick:
    def test_it_drains_everything_due(self, tenancy: Tenancy) -> None:
        for _ in range(5):
            make_action(tenancy.workspace, type=PROBE)
        out = StringIO()

        with temporary_handler(PROBE, _noop):
            call_command("tick", "--batch-size", "2", stdout=out)

        assert "5 action(s)" in out.getvalue()
        assert ScheduledAction.objects.for_workspace(tenancy.workspace).filter(status=ActionStatus.DONE).count() == 5

    def test_it_leaves_work_that_is_not_due_yet(self, tenancy: Tenancy) -> None:
        make_action(tenancy.workspace, type=PROBE, run_at=timezone.now() + timedelta(hours=1))
        out = StringIO()

        with temporary_handler(PROBE, _noop):
            call_command("tick", stdout=out)

        assert "Processed 0 action(s)" in out.getvalue()

    def test_a_zero_second_budget_still_runs_one_batch(self, tenancy: Tenancy) -> None:
        """The deadline is checked between batches: a claimed row is never abandoned."""
        make_action(tenancy.workspace, type=PROBE)
        out = StringIO()

        with temporary_handler(PROBE, _noop):
            call_command("tick", "--max-seconds", "0", stdout=out)

        assert "1 done" in out.getvalue()

    @pytest.mark.parametrize("batch_size", ["0", "-1"])
    def test_a_non_positive_batch_size_is_refused(self, batch_size: str) -> None:
        from django.core.management import CommandError

        with pytest.raises((CommandError, SystemExit, ValueError)):
            call_command("tick", "--batch-size", batch_size, stdout=StringIO())

    def test_it_bootstraps_the_housekeeping_chain(self) -> None:
        call_command("tick", stdout=StringIO())
        assert ScheduledAction.objects.unscoped().filter(type="housekeeping").exists()
