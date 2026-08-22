"""Stale-execution expiry, and the two ways it reaches the hourly sweep.

The registration test matters as much as the behaviour one: this job is wired
both by ``@register_housekeeping_job`` here and by a dotted path already sitting
in ``apps/queueing/housekeeping.py``'s ``OPTIONAL_JOB_PATHS``, and the failure
mode of that belt-and-braces arrangement would be running the sweep twice.
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.flows.engine import start_flow
from apps.flows.engine.results import Wait
from apps.flows.housekeeping import STALE_AFTER, expire_stale_executions
from apps.flows.models import ExecutionStatus, FlowExecution, StartedBy
from apps.flows.tests.support import contact_for, graph, node, node_runtime, published_flow
from apps.queueing.housekeeping import housekeeping_jobs

NOOP_ACTION = {"actions": [{"verb": "remove_tag", "tag": "not-a-tag-here"}]}
WAIT_CONFIG = {"type": "buttons", "token": "t1", "handles": {}}


def _age(execution: FlowExecution, days: int) -> None:
    """Backdate ``updated_at``, which ``auto_now`` will not let a save do."""
    FlowExecution.objects.unscoped().filter(pk=execution.pk).update(updated_at=timezone.now() - timedelta(days=days))


def _parked(workspace, name="Parked"):
    flow = published_flow(workspace, graph([node("a", "action", NOOP_ACTION)]), name=name)
    contact = contact_for(workspace, first_name=name)
    with node_runtime("action", lambda ctx: Wait(WAIT_CONFIG)):
        return start_flow(contact, flow, started_by=StartedBy.API)


class TestRegistration:
    def test_the_job_is_in_the_hourly_sweep(self):
        assert "expire_stale_executions" in housekeeping_jobs()

    def test_it_is_registered_exactly_once(self):
        """The decorator and ``OPTIONAL_JOB_PATHS`` must not both add it."""
        jobs = housekeeping_jobs()
        assert jobs["expire_stale_executions"] is expire_stale_executions


@pytest.mark.django_db
class TestExpiry:
    def test_a_wait_older_than_thirty_days_expires(self, tenancy):
        execution = _parked(tenancy.workspace)
        _age(execution, STALE_AFTER.days + 1)

        expire_stale_executions()

        execution.refresh_from_db()
        assert execution.status == ExecutionStatus.EXPIRED
        assert execution.wait_config == {}

    def test_a_recent_wait_is_left_alone(self, tenancy):
        execution = _parked(tenancy.workspace)
        _age(execution, STALE_AFTER.days - 1)

        expire_stale_executions()

        execution.refresh_from_db()
        assert execution.status == ExecutionStatus.WAITING_REPLY

    def test_a_stuck_running_execution_is_not_this_jobs_business(self, tenancy):
        """A ``running`` row means a worker died mid-step; that is zombie recovery's."""
        flow = published_flow(tenancy.workspace, graph([node("a", "action", NOOP_ACTION)]))
        contact = contact_for(tenancy.workspace)
        execution = start_flow(contact, flow, started_by=StartedBy.API)
        FlowExecution.objects.unscoped().filter(pk=execution.pk).update(
            status=ExecutionStatus.RUNNING, updated_at=timezone.now() - timedelta(days=90)
        )

        expire_stale_executions()

        execution.refresh_from_db()
        assert execution.status == ExecutionStatus.RUNNING

    def test_it_sweeps_every_tenant(self, tenancy, other_tenancy):
        """Housekeeping is deployment-wide; that is what ``.unscoped()`` is for."""
        ours = _parked(tenancy.workspace, name="Ours")
        theirs = _parked(other_tenancy.workspace, name="Theirs")
        _age(ours, 40)
        _age(theirs, 40)

        summary = expire_stale_executions()

        ours.refresh_from_db()
        theirs.refresh_from_db()
        assert ours.status == theirs.status == ExecutionStatus.EXPIRED
        assert "expired 2" in summary

    def test_running_it_twice_changes_nothing_the_second_time(self, tenancy):
        """Every housekeeping job must be idempotent — the sweep retries as a whole."""
        execution = _parked(tenancy.workspace)
        _age(execution, 40)

        assert "expired 1" in expire_stale_executions()
        assert "expired 0" in expire_stale_executions()
