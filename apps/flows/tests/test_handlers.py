"""The three queue action types, driven through the real worker.

Running them through :func:`apps.queueing.worker.process_action` rather than
calling the handler directly is deliberate: the worker is what takes the contact
lock, opens the transaction and marks the row, and the handlers are written
assuming all three. A test that called the function bare would pass with the
registration broken and the lock absent.
"""

from typing import Any

import pytest
from django.utils import timezone

from apps.flows.engine.results import Wait
from apps.flows.models import ExecutionStatus, FlowExecution, StartedBy
from apps.flows.tests.support import contact_for, edge, graph, node, node_runtime, published_flow
from apps.queueing.models import ActionStatus, ActionType, ScheduledAction
from apps.queueing.registry import get_handler, schedule
from apps.queueing.worker import process_action

NOOP_ACTION = {"actions": [{"verb": "remove_tag", "tag": "not-a-tag-here"}]}
TAG_ACTION = {"actions": [{"verb": "add_tag", "tag": "resumed"}]}
WAIT_CONFIG = {"type": "buttons", "token": "t1", "handles": {}}


def _run(action: ScheduledAction) -> ScheduledAction:
    """Claim-and-run one action the way the worker does, then re-read it."""
    action.status = ActionStatus.RUNNING
    action.attempts = 1
    action.save(update_fields=["status", "attempts", "updated_at"])
    process_action(action)
    action.refresh_from_db()
    return action


#: A node type whose spec exposes both `default` and `timeout` — which is what
#: a followup timer needs somewhere to land. Its real runtime is PR 2's; here it
#: is stubbed to park, because what is under test is the queue wiring.
QUESTION = {"question": "Your email?", "reply_type": "text", "target": {"type": "custom_field", "key": "answer"}}


def _waiting_flow(workspace: Any):
    document = graph(
        [node("a", "data_collection", QUESTION), node("b", "action", TAG_ACTION, x=200)],
        [edge("a", "default", "b"), edge("a", "timeout", "b")],
    )
    return published_flow(workspace, document)


class TestRegistration:
    def test_the_three_types_are_claimed(self):
        for action_type in (ActionType.START_FLOW, ActionType.RESUME_EXECUTION, ActionType.FOLLOWUP_TIMER):
            assert get_handler(action_type) is not None


@pytest.mark.django_db
class TestStartFlowAction:
    def test_it_starts_the_flow(self, tenancy):
        flow = published_flow(tenancy.workspace, graph([node("a", "action", TAG_ACTION)]))
        contact = contact_for(tenancy.workspace)
        action = schedule(
            ActionType.START_FLOW,
            timezone.now(),
            {"contact_id": str(contact.pk), "flow_id": str(flow.pk), "started_by": StartedBy.API},
            workspace=tenancy.workspace,
            contact=contact,
        )

        assert _run(action).status == ActionStatus.DONE
        execution = FlowExecution.objects.for_workspace(tenancy.workspace).get()
        assert execution.status == ExecutionStatus.COMPLETED
        assert {tag.name for tag in contact.tags.all()} == {"resumed"}

    def test_an_unpublished_flow_is_dropped_rather_than_retried(self, tenancy):
        """Five attempts over six hours cannot make a draft publishable."""
        from apps.flows.services import create_flow

        flow = create_flow(workspace=tenancy.workspace, name="Draft")
        contact = contact_for(tenancy.workspace)
        action = schedule(
            ActionType.START_FLOW,
            timezone.now(),
            {"contact_id": str(contact.pk), "flow_id": str(flow.pk)},
            workspace=tenancy.workspace,
            contact=contact,
        )

        assert _run(action).status == ActionStatus.DONE
        assert not FlowExecution.objects.for_workspace(tenancy.workspace).exists()

    def test_a_payload_naming_another_tenants_flow_resolves_to_nothing(self, tenancy, other_tenancy):
        """Ids in a payload are ids, not trusted objects."""
        theirs = published_flow(other_tenancy.workspace, graph([node("a", "action", TAG_ACTION)]))
        contact = contact_for(tenancy.workspace)
        action = schedule(
            ActionType.START_FLOW,
            timezone.now(),
            {"contact_id": str(contact.pk), "flow_id": str(theirs.pk)},
            workspace=tenancy.workspace,
            contact=contact,
        )

        assert _run(action).status == ActionStatus.DONE
        assert not FlowExecution.objects.for_workspace(tenancy.workspace).exists()
        assert {tag.name for tag in contact.tags.all()} == set()

    def test_a_version_belonging_to_another_flow_is_dropped_not_retried(self, tenancy):
        """A permanent argument fault: five attempts over six hours cannot fix it."""
        from apps.flows.services import latest_version

        flow = published_flow(tenancy.workspace, graph([node("a", "action", TAG_ACTION)]))
        other = published_flow(tenancy.workspace, graph([node("a", "action", TAG_ACTION)]), name="Other")
        contact = contact_for(tenancy.workspace)
        action = schedule(
            ActionType.START_FLOW,
            timezone.now(),
            {
                "contact_id": str(contact.pk),
                "flow_id": str(flow.pk),
                "flow_version_id": str(latest_version(other).pk),
            },
            workspace=tenancy.workspace,
            contact=contact,
        )

        assert _run(action).status == ActionStatus.DONE
        assert not FlowExecution.objects.for_workspace(tenancy.workspace).exists()

    def test_a_missing_contact_is_dropped(self, tenancy):
        flow = published_flow(tenancy.workspace, graph([node("a", "action", TAG_ACTION)]))
        action = schedule(
            ActionType.START_FLOW,
            timezone.now(),
            {"contact_id": "0192f000-0000-7000-8000-0000000000ff", "flow_id": str(flow.pk)},
            workspace=tenancy.workspace,
        )

        assert _run(action).status == ActionStatus.DONE


@pytest.mark.django_db
class TestResumeActions:
    def _parked(self, tenancy):
        from apps.flows.engine import start_flow

        flow = _waiting_flow(tenancy.workspace)
        contact = contact_for(tenancy.workspace)
        with node_runtime("data_collection", lambda ctx: Wait(WAIT_CONFIG)):
            execution = start_flow(contact, flow, started_by=StartedBy.API)
        return contact, execution

    def test_resume_execution_follows_the_default_handle(self, tenancy):
        contact, execution = self._parked(tenancy)
        action = schedule(
            ActionType.RESUME_EXECUTION,
            timezone.now(),
            {"execution_id": str(execution.pk), "handle": "default", "token": "t1"},
            workspace=tenancy.workspace,
            contact=contact,
        )

        assert _run(action).status == ActionStatus.DONE
        execution.refresh_from_db()
        assert execution.status == ExecutionStatus.COMPLETED
        assert {tag.name for tag in contact.tags.all()} == {"resumed"}

    def test_the_followup_timer_defaults_to_the_timeout_handle(self, tenancy):
        contact, execution = self._parked(tenancy)
        action = schedule(
            ActionType.FOLLOWUP_TIMER,
            timezone.now(),
            {"execution_id": str(execution.pk), "token": "t1"},
            workspace=tenancy.workspace,
            contact=contact,
        )

        assert _run(action).status == ActionStatus.DONE
        execution.refresh_from_db()
        assert execution.current_node_id == "b"

    def test_a_stale_token_is_a_no_op_rather_than_a_failure(self, tenancy):
        """A timer racing a reply must lose quietly, not retry five times."""
        contact, execution = self._parked(tenancy)
        action = schedule(
            ActionType.FOLLOWUP_TIMER,
            timezone.now(),
            {"execution_id": str(execution.pk), "token": "stale"},
            workspace=tenancy.workspace,
            contact=contact,
        )

        assert _run(action).status == ActionStatus.DONE
        execution.refresh_from_db()
        assert execution.status == ExecutionStatus.WAITING_REPLY

    def test_a_vanished_execution_is_dropped(self, tenancy):
        contact = contact_for(tenancy.workspace)
        action = schedule(
            ActionType.RESUME_EXECUTION,
            timezone.now(),
            {"execution_id": "0192f000-0000-7000-8000-0000000000ff"},
            workspace=tenancy.workspace,
            contact=contact,
        )

        assert _run(action).status == ActionStatus.DONE
