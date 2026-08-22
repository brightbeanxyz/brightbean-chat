"""SPEC §21's acceptance criterion, and §3's "single invariant".

    concurrent webhooks for one contact never interleave steps (test with 50
    parallel events)

This is one of the five conditions on the Layer-3 gate, so it is tested against
the real thing: 50 OS threads, 50 database connections, the real advisory lock.
Nothing is mocked and nothing is serialised by the test harness — if the lock
were missing, this file would fail.

**How interleaving is detected.** A node that merely counted would pass whether
or not the lock worked; a counter is atomic-ish under the GIL and would only
catch a lost update. So the stub node instead *straddles* a window: it appends
"enter", sleeps long enough for every other thread to get in if it can, then
appends "leave". Under the lock the trace is a clean sequence of enter/leave
pairs; without it the sleeps overlap and pairs nest. :func:`_interleavings`
counts the nesting, and the assertion is that there is none.

``transaction=True`` because each thread needs its own committing connection —
the ordinary ``django_db`` fixture wraps everything in one transaction that no
other thread can see into, which would hide exactly what this measures.
"""

import threading
import time
from typing import Any

import pytest
from django.db import connections

from apps.flows.engine import resume_execution, start_flow
from apps.flows.engine.results import Continue, Wait
from apps.flows.models import ExecutionStatus, FlowExecution, StartedBy
from apps.flows.tests.support import contact_for, edge, graph, node, node_runtime, published_flow
from apps.queueing.locks import LockOutsideTransactionError
from tests.support import create_tenancy

PARALLEL = 50
NOOP_ACTION = {"actions": [{"verb": "remove_tag", "tag": "not-a-tag-here"}]}
WAIT_CONFIG = {"type": "buttons", "token": "t1", "handles": {}}

#: Long enough that 50 threads racing for the lock would visibly overlap, short
#: enough that the whole test stays under a couple of seconds.
STRADDLE = 0.01


def _interleavings(trace: list[tuple[str, str]]) -> int:
    """How many times a second thread entered before the first one left."""
    depth = 0
    overlaps = 0
    for _thread, phase in trace:
        if phase == "enter":
            if depth:
                overlaps += 1
            depth += 1
        else:
            depth -= 1
    return overlaps


@pytest.mark.django_db(transaction=True)
class TestOneStepPerContact:
    def test_fifty_parallel_resumes_never_interleave(self) -> None:
        tenancy = create_tenancy("engine-lock")
        # Two nodes: the entry waits, and every resume runs the second one. The
        # second has no outgoing edge, so each resume is exactly one block —
        # which is what makes an overlap in the trace unambiguous.
        document = graph(
            [node("a", "action", NOOP_ACTION), node("b", "action", NOOP_ACTION, x=200)],
            [edge("a", "default", "b")],
        )
        flow = published_flow(tenancy.workspace, document)
        contact = contact_for(tenancy.workspace)

        trace: list[tuple[str, str]] = []
        trace_lock = threading.Lock()

        def _straddle(ctx: Any) -> Any:
            name = threading.current_thread().name
            with trace_lock:
                trace.append((name, "enter"))
            time.sleep(STRADDLE)
            with trace_lock:
                trace.append((name, "leave"))
            return Wait(WAIT_CONFIG) if ctx.node_id == "b" else Continue()

        with node_runtime("action", _straddle):
            execution = start_flow(contact, flow, started_by=StartedBy.API)
            assert execution.status == ExecutionStatus.WAITING_REPLY

            errors: list[BaseException] = []

            def _resume() -> None:
                try:
                    resume_execution(execution, handle="default")
                except BaseException as exc:  # noqa: BLE001 - re-raised on the main thread
                    errors.append(exc)
                finally:
                    connections.close_all()

            threads = [threading.Thread(target=_resume, name=f"resume-{index}") for index in range(PARALLEL)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=120)
                assert not thread.is_alive(), f"{thread.name} did not finish"

        assert errors == []
        assert _interleavings(trace) == 0, f"steps overlapped: {trace[:12]}"

    def test_only_one_of_fifty_resumes_actually_advances(self) -> None:
        """The other 49 find the execution already moved on and no-op.

        The lock serialises them; what stops the 49 followers from each running
        the flow again is the status re-read inside the lock. Both halves are
        needed, and this asserts the second one.
        """
        tenancy = create_tenancy("engine-once")
        document = graph(
            [
                node("a", "action", NOOP_ACTION),
                node("b", "action", {"actions": [{"verb": "add_tag", "tag": "x"}]}, x=200),
            ],
            [edge("a", "default", "b")],
        )
        flow = published_flow(tenancy.workspace, document)
        contact = contact_for(tenancy.workspace)

        runs: list[str] = []
        runs_lock = threading.Lock()

        def _count(ctx: Any) -> Any:
            if ctx.node_id == "a":
                return Wait(WAIT_CONFIG)
            with runs_lock:
                runs.append(ctx.node_id)
            return Continue()

        with node_runtime("action", _count):
            execution = start_flow(contact, flow, started_by=StartedBy.API)

            def _resume() -> None:
                try:
                    resume_execution(execution, handle="default")
                finally:
                    connections.close_all()

            threads = [threading.Thread(target=_resume) for _ in range(PARALLEL)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=120)

        assert runs == ["b"]
        execution.refresh_from_db()
        assert execution.status == ExecutionStatus.COMPLETED
        assert FlowExecution.objects.for_workspace(tenancy.workspace).count() == 1

    def test_a_stale_token_does_not_resume_a_reused_wait(self) -> None:
        """A followup timer that fires after its wait was re-entered is a no-op."""
        tenancy = create_tenancy("engine-token")
        flow = published_flow(tenancy.workspace, graph([node("a", "action", NOOP_ACTION)]))
        contact = contact_for(tenancy.workspace)

        with node_runtime("action", lambda ctx: Wait({"type": "buttons", "token": "fresh", "handles": {}})):
            execution = start_flow(contact, flow, started_by=StartedBy.API)
            resume_execution(execution, handle="default", token="stale")

        execution.refresh_from_db()
        assert execution.status == ExecutionStatus.WAITING_REPLY


@pytest.mark.django_db(transaction=True)
class TestLockDiscipline:
    def test_the_lock_helper_refuses_outside_a_transaction(self) -> None:
        """Pinning the dependency: the engine's invariant is that refusal.

        ``pg_advisory_xact_lock`` outside a transaction is taken and dropped
        before the work it protects starts — no error, no mutual exclusion. The
        engine's entry points open their own ``atomic()``; this asserts that the
        thing they rely on would notice if one of them stopped.
        """
        from apps.queueing.locks import contact_lock

        with pytest.raises(LockOutsideTransactionError), contact_lock("0192f000-0000-7000-8000-000000000001"):
            pass
