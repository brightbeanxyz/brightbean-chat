"""``FlowExecution``: the invariants the database holds, not the ones code holds.

SPEC §5's partial unique index and ``ContactScopedModel``'s derived workspace are
both things that must be true even if every service in the app forgets them, so
they are asserted against the table rather than through the engine.
"""

from typing import Any

import pytest
from django.db import IntegrityError, transaction

from apps.contacts.errors import WorkspaceMismatchError
from apps.flows.models import LIVE_STATUSES, ExecutionStatus, FlowExecution, StartedBy
from apps.flows.services import create_flow, latest_version
from apps.flows.tests.support import contact_for


def _execution(workspace: Any, contact: Any, flow: Any, **fields: Any) -> FlowExecution:
    version = latest_version(flow)
    assert version is not None, "create_flow() always makes version 1"
    return FlowExecution.objects.create(
        workspace=workspace,
        flow=flow,
        flow_version=version,
        contact=contact,
        current_node_id="n1",
        started_by=StartedBy.API,
        **fields,
    )


@pytest.mark.django_db
class TestLiveExecutionIndex:
    def test_one_live_execution_per_contact_and_flow(self, tenancy):
        flow = create_flow(workspace=tenancy.workspace, name="Welcome")
        contact = contact_for(tenancy.workspace)
        _execution(tenancy.workspace, contact, flow)

        with pytest.raises(IntegrityError), transaction.atomic():
            _execution(tenancy.workspace, contact, flow)

    @pytest.mark.parametrize("status", sorted(LIVE_STATUSES))
    def test_every_live_status_takes_the_slot(self, tenancy, status):
        flow = create_flow(workspace=tenancy.workspace, name="Welcome")
        contact = contact_for(tenancy.workspace)
        _execution(tenancy.workspace, contact, flow, status=status)

        with pytest.raises(IntegrityError), transaction.atomic():
            _execution(tenancy.workspace, contact, flow, status=status)

    @pytest.mark.parametrize("status", [ExecutionStatus.COMPLETED, ExecutionStatus.FAILED, ExecutionStatus.EXPIRED])
    def test_terminal_executions_do_not_take_the_slot(self, tenancy, status):
        """History accumulates. A contact who ran a flow last week can run it again."""
        flow = create_flow(workspace=tenancy.workspace, name="Welcome")
        contact = contact_for(tenancy.workspace)
        _execution(tenancy.workspace, contact, flow, status=status)
        _execution(tenancy.workspace, contact, flow, status=status)
        _execution(tenancy.workspace, contact, flow)

        rows = FlowExecution.objects.for_workspace(tenancy.workspace).filter(contact=contact)
        assert rows.count() == 3

    def test_the_index_is_per_flow_not_per_contact(self, tenancy):
        """SPEC §5's index alone permits two flows at once.

        The stricter §9.2/§22 rule — one live execution per contact across every
        flow — is ``engine.start_flow``'s, under the contact lock, and is
        asserted in ``test_supersede``. Pinning the difference here keeps the two
        from being confused for one guarantee.
        """
        first = create_flow(workspace=tenancy.workspace, name="One")
        second = create_flow(workspace=tenancy.workspace, name="Two")
        contact = contact_for(tenancy.workspace)
        _execution(tenancy.workspace, contact, first)
        _execution(tenancy.workspace, contact, second)

        live = FlowExecution.objects.for_workspace(tenancy.workspace).filter(
            contact=contact, status__in=sorted(LIVE_STATUSES)
        )
        assert live.count() == 2


@pytest.mark.django_db
class TestTenancy:
    def test_workspace_is_derived_from_the_contact(self, tenancy):
        flow = create_flow(workspace=tenancy.workspace, name="Welcome")
        contact = contact_for(tenancy.workspace)
        execution = FlowExecution(
            # Deliberately wrong, and deliberately ignored: ContactScopedModel
            # derives the workspace rather than trusting the caller.
            workspace=None,
            flow=flow,
            flow_version=latest_version(flow),
            contact=contact,
        )
        execution.save()
        assert execution.workspace_id == tenancy.workspace.pk

    def test_a_contact_cannot_be_run_through_another_tenants_flow(self, tenancy, other_tenancy):
        flow = create_flow(workspace=other_tenancy.workspace, name="Theirs")
        contact = contact_for(tenancy.workspace)
        execution = FlowExecution(flow=flow, flow_version=latest_version(flow), contact=contact)

        with pytest.raises(WorkspaceMismatchError):
            execution.save()

    def test_queries_refuse_to_run_unscoped(self, tenancy):
        from apps.common.scoping import UnscopedQueryError

        with pytest.raises(UnscopedQueryError):
            FlowExecution.objects.filter(status=ExecutionStatus.RUNNING).count()


@pytest.mark.django_db
class TestFlowColumn:
    def test_flow_is_kept_in_step_with_the_version(self, tenancy):
        """One fact written twice; the model owns keeping the two the same."""
        flow = create_flow(workspace=tenancy.workspace, name="Welcome")
        other = create_flow(workspace=tenancy.workspace, name="Other")
        contact = contact_for(tenancy.workspace)

        execution = FlowExecution(
            flow=other,
            flow_version=latest_version(flow),
            contact=contact,
        )
        execution.save()
        assert execution.flow_id == flow.pk


class TestStartedBy:
    def test_stamp_composes_kind_and_id(self):
        assert StartedBy.stamp(StartedBy.TRIGGER, "abc") == "trigger:abc"
        assert StartedBy.stamp(StartedBy.API) == "api"

    def test_an_unknown_kind_is_refused(self):
        """The vocabulary is closed so a consumer can parse the column."""
        with pytest.raises(ValueError, match="not a started_by kind"):
            StartedBy.stamp("whatever", "abc")
