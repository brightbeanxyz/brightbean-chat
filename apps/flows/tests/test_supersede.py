"""SPEC §22's decision, and the draft-preview run that #12 needs.

    One live execution per contact across all flows; new start supersedes.

The database only holds the per-flow half of that (SPEC §5's partial unique
index over ``(contact, flow)``); the cross-flow half is
:func:`apps.flows.engine.start_flow`, under the contact advisory lock. So the
interesting assertions here are about a *second flow* superseding the first,
which no constraint would catch.
"""

import pytest
from django.utils import timezone

from apps.flows.engine import start_flow
from apps.flows.engine.results import Wait
from apps.flows.models import LIVE_STATUSES, ExecutionStatus, FlowExecution, StartedBy
from apps.flows.services import latest_version, save_draft
from apps.flows.tests.support import contact_for, graph, node, node_runtime, published_flow
from apps.queueing.models import ActionStatus, ActionType
from apps.queueing.registry import schedule

NOOP_ACTION = {"actions": [{"verb": "remove_tag", "tag": "not-a-tag-here"}]}
WAIT_CONFIG = {"type": "buttons", "token": "t1", "handles": {}}


def _waiting(workspace, contact, flow):
    """Start ``flow`` and leave it parked at its only node."""
    with node_runtime("action", lambda ctx: Wait(WAIT_CONFIG)):
        return start_flow(contact, flow, started_by=StartedBy.API)


@pytest.mark.django_db
class TestSupersede:
    def test_starting_the_same_flow_again_expires_the_old_run(self, tenancy):
        flow = published_flow(tenancy.workspace, graph([node("a", "action", NOOP_ACTION)]))
        contact = contact_for(tenancy.workspace)
        first = _waiting(tenancy.workspace, contact, flow)

        second = _waiting(tenancy.workspace, contact, flow)

        first.refresh_from_db()
        assert first.status == ExecutionStatus.EXPIRED
        assert second.status == ExecutionStatus.WAITING_REPLY
        assert first.pk != second.pk

    def test_starting_a_different_flow_also_supersedes(self, tenancy):
        """The half no index enforces — SPEC §9.2's "any flow"."""
        first_flow = published_flow(tenancy.workspace, graph([node("a", "action", NOOP_ACTION)]), name="One")
        second_flow = published_flow(tenancy.workspace, graph([node("b", "action", NOOP_ACTION)]), name="Two")
        contact = contact_for(tenancy.workspace)
        first = _waiting(tenancy.workspace, contact, first_flow)

        _waiting(tenancy.workspace, contact, second_flow)

        first.refresh_from_db()
        assert first.status == ExecutionStatus.EXPIRED
        live = FlowExecution.objects.for_workspace(tenancy.workspace).filter(
            contact=contact, status__in=sorted(LIVE_STATUSES)
        )
        assert live.count() == 1

    def test_another_contacts_execution_is_untouched(self, tenancy):
        flow = published_flow(tenancy.workspace, graph([node("a", "action", NOOP_ACTION)]))
        one = contact_for(tenancy.workspace, first_name="One")
        two = contact_for(tenancy.workspace, first_name="Two")
        theirs = _waiting(tenancy.workspace, one, flow)

        _waiting(tenancy.workspace, two, flow)

        theirs.refresh_from_db()
        assert theirs.status == ExecutionStatus.WAITING_REPLY

    def test_a_superseded_executions_timers_are_cancelled(self, tenancy):
        """A cancelled row never wakes a worker at all — cheaper than a no-op resume."""
        flow = published_flow(tenancy.workspace, graph([node("a", "action", NOOP_ACTION)]))
        contact = contact_for(tenancy.workspace)
        first = _waiting(tenancy.workspace, contact, flow)
        timer = schedule(
            ActionType.FOLLOWUP_TIMER,
            timezone.now() + timezone.timedelta(hours=1),
            {"execution_id": str(first.pk), "handle": "timeout", "token": "t1"},
            workspace=tenancy.workspace,
            contact=contact,
        )

        _waiting(tenancy.workspace, contact, flow)

        timer.refresh_from_db()
        assert timer.status == ActionStatus.CANCELLED

    def test_a_pending_start_flow_action_is_left_alone(self, tenancy):
        """A future start somebody scheduled deliberately is not this rule's business."""
        flow = published_flow(tenancy.workspace, graph([node("a", "action", NOOP_ACTION)]))
        contact = contact_for(tenancy.workspace)
        _waiting(tenancy.workspace, contact, flow)
        future = schedule(
            ActionType.START_FLOW,
            timezone.now() + timezone.timedelta(days=1),
            {"contact_id": str(contact.pk), "flow_id": str(flow.pk)},
            workspace=tenancy.workspace,
            contact=contact,
        )

        _waiting(tenancy.workspace, contact, flow)

        future.refresh_from_db()
        assert future.status == ActionStatus.PENDING

    def test_terminal_executions_are_not_re_expired(self, tenancy):
        flow = published_flow(tenancy.workspace, graph([node("a", "action", NOOP_ACTION)]))
        contact = contact_for(tenancy.workspace)
        completed = start_flow(contact, flow, started_by=StartedBy.API)
        assert completed.status == ExecutionStatus.COMPLETED

        start_flow(contact, flow, started_by=StartedBy.API)

        completed.refresh_from_db()
        assert completed.status == ExecutionStatus.COMPLETED


@pytest.mark.django_db
class TestDraftPreview:
    """#12's "test on Telegram": run the draft, flag it, keep it out of stats."""

    def test_an_explicit_draft_version_runs_the_draft_graph(self, tenancy):
        published = graph([node("a", "action", {"actions": [{"verb": "add_tag", "tag": "published"}]})])
        flow = published_flow(tenancy.workspace, published)
        draft = save_draft(flow, graph([node("a", "action", {"actions": [{"verb": "add_tag", "tag": "draft"}]})]))
        contact = contact_for(tenancy.workspace)

        execution = start_flow(contact, flow, started_by=StartedBy.PREVIEW, flow_version=draft)

        assert execution.flow_version_id == draft.pk
        assert {tag.name for tag in contact.tags.all()} == {"draft"}

    def test_a_draft_run_is_flagged_preview(self, tenancy):
        flow = published_flow(tenancy.workspace, graph([node("a", "action", NOOP_ACTION)]))
        draft = save_draft(flow, graph([node("a", "action", NOOP_ACTION)]))
        contact = contact_for(tenancy.workspace)

        execution = start_flow(contact, flow, started_by=StartedBy.PREVIEW, flow_version=draft)

        assert execution.preview is True

    def test_a_published_run_is_not_flagged(self, tenancy):
        flow = published_flow(tenancy.workspace, graph([node("a", "action", NOOP_ACTION)]))
        contact = contact_for(tenancy.workspace)

        execution = start_flow(contact, flow, started_by=StartedBy.API)

        assert execution.preview is False

    def test_naming_the_published_version_explicitly_is_not_a_preview(self, tenancy):
        flow = published_flow(tenancy.workspace, graph([node("a", "action", NOOP_ACTION)]))
        contact = contact_for(tenancy.workspace)

        execution = start_flow(contact, flow, started_by=StartedBy.API, flow_version=latest_version(flow))

        assert execution.preview is False

    def test_preview_runs_are_excluded_from_a_stats_query(self, tenancy):
        """The flag L7-A's per-node counters filter on (SPEC §18)."""
        flow = published_flow(tenancy.workspace, graph([node("a", "action", NOOP_ACTION)]))
        draft = save_draft(flow, graph([node("a", "action", NOOP_ACTION)]))
        one = contact_for(tenancy.workspace, first_name="One")
        two = contact_for(tenancy.workspace, first_name="Two")
        start_flow(one, flow, started_by=StartedBy.API)
        start_flow(two, flow, started_by=StartedBy.PREVIEW, flow_version=draft)

        countable = FlowExecution.objects.for_workspace(tenancy.workspace).filter(flow=flow, preview=False)
        assert countable.count() == 1

    def test_a_preview_supersedes_like_any_other_start(self, tenancy):
        """A preview is a real run with real sends, so §22 applies to it too."""
        flow = published_flow(tenancy.workspace, graph([node("a", "action", NOOP_ACTION)]))
        draft = save_draft(flow, graph([node("a", "action", NOOP_ACTION)]))
        contact = contact_for(tenancy.workspace)
        live = _waiting(tenancy.workspace, contact, flow)

        start_flow(contact, flow, started_by=StartedBy.PREVIEW, flow_version=draft)

        live.refresh_from_db()
        assert live.status == ExecutionStatus.EXPIRED
