"""Taking a live flow offline — the builder's "Set offline".

Two promises, and most of this file is the second one. Nothing new starts an
offline flow, by any route; and nothing already in it carries on, so somebody
who got the menu a minute ago and taps a reply afterwards gets nothing. The
entry points that refuse it — trigger matching, the public API, a contact's
page — are asserted next to their own tests; what is here is the lifecycle and
the engine half.
"""

import threading

import pytest
from django.db import connection
from django.utils import timezone

import apps.flows.engine as engine
from apps.flows.engine import FlowNotRunnableError, expire_flow_executions, start_flow
from apps.flows.engine.results import Wait
from apps.flows.models import ExecutionStatus, FlowStatus, FlowVersion, StartedBy
from apps.flows.services import (
    FlowNotLiveError,
    archive_flow,
    create_flow,
    latest_version,
    publish,
    published_version,
    restore_flow,
    save_draft,
    take_offline,
)
from apps.flows.tests.support import contact_for, graph, node, node_runtime, published_flow
from apps.queueing.models import ActionStatus, ActionType
from apps.queueing.registry import schedule

NOOP_ACTION = {"actions": [{"verb": "remove_tag", "tag": "not-a-tag-here"}]}
WAIT_CONFIG = {"type": "buttons", "token": "t1", "handles": {}}


def _live(workspace, name="Welcome"):
    return published_flow(workspace, graph([node("a", "action", NOOP_ACTION)]), name=name)


def _waiting(contact, flow):
    """Start ``flow`` and leave it parked at its only node, as a menu would."""
    with node_runtime("action", lambda ctx: Wait(WAIT_CONFIG)):
        return start_flow(contact, flow, started_by=StartedBy.API)


def _timer(workspace, contact, execution, action_type=ActionType.FOLLOWUP_TIMER):
    return schedule(
        action_type,
        timezone.now() + timezone.timedelta(hours=1),
        {"execution_id": str(execution.pk), "handle": "timeout", "token": "t1"},
        workspace=workspace,
        contact=contact,
    )


@pytest.mark.django_db
class TestTakeOffline:
    def test_it_unpublishes_and_says_offline(self, tenancy):
        flow = _live(tenancy.workspace)

        take_offline(flow)

        flow.refresh_from_db()
        assert flow.status == FlowStatus.OFFLINE
        assert published_version(flow) is None
        assert not FlowVersion.objects.for_workspace(tenancy.workspace).filter(flow=flow, published=True).exists()

    def test_the_callers_instance_is_kept_honest(self, tenancy):
        flow = _live(tenancy.workspace)

        take_offline(flow)

        assert flow.status == FlowStatus.OFFLINE

    @pytest.mark.parametrize("status", [FlowStatus.DRAFT, FlowStatus.OFFLINE, FlowStatus.ARCHIVED])
    def test_only_a_live_flow_can_be_taken_offline(self, tenancy, status):
        flow = create_flow(workspace=tenancy.workspace, name="Not live")
        flow.status = status
        flow.save(update_fields=["status", "updated_at"])

        with pytest.raises(FlowNotLiveError):
            take_offline(flow)

    def test_triggers_are_left_as_they_were(self, tenancy):
        """An offline flow is not matched whatever its triggers say, and pausing
        them would lose which ones somebody had paused on purpose."""
        from apps.flows.models import Trigger, TriggerType

        flow = _live(tenancy.workspace)
        on = Trigger.objects.create(workspace=tenancy.workspace, flow=flow, type=TriggerType.API, enabled=True)
        off = Trigger.objects.create(
            workspace=tenancy.workspace, flow=flow, type=TriggerType.API, enabled=False, priority=10
        )

        take_offline(flow)

        on.refresh_from_db()
        off.refresh_from_db()
        assert (on.enabled, off.enabled) == (True, False)

    def test_setting_it_live_again_is_an_ordinary_publish(self, tenancy):
        flow = _live(tenancy.workspace)
        before = latest_version(flow)
        take_offline(flow)

        result = publish(flow)

        flow.refresh_from_db()
        assert flow.status == FlowStatus.ACTIVE
        # The same version comes back, not a copy of it.
        assert result.version.pk == before.pk
        assert published_version(flow).pk == before.pk

    def test_edits_made_while_offline_are_what_goes_live(self, tenancy):
        flow = _live(tenancy.workspace)
        take_offline(flow)
        edited = save_draft(flow, graph([node("b", "action", NOOP_ACTION)]))

        result = publish(flow)

        assert result.version.pk == edited.pk

    def test_archiving_and_restoring_an_offline_flow_brings_back_a_draft(self, tenancy):
        """Restore puts a flow back live only when something is published, and
        an offline flow has nothing published — so it cannot come back live by
        the side door."""
        flow = _live(tenancy.workspace)
        take_offline(flow)
        archive_flow(flow)

        restore_flow(flow)

        assert flow.status == FlowStatus.DRAFT

    def test_the_version_that_was_live_stays_frozen(self, tenancy):
        """Conversations ran on it, so an edit made while offline opens the next
        version rather than rewriting that one in place."""
        flow = _live(tenancy.workspace)
        was_live = latest_version(flow)
        original = was_live.graph_json
        take_offline(flow)

        edited = save_draft(flow, graph([node("b", "action", NOOP_ACTION)]))

        was_live.refresh_from_db()
        assert edited.pk != was_live.pk
        assert edited.version == was_live.version + 1
        assert was_live.graph_json == original
        assert was_live.published_at is not None

    def test_going_live_again_keeps_when_it_first_went_live(self, tenancy):
        flow = _live(tenancy.workspace)
        first = latest_version(flow).published_at
        take_offline(flow)

        publish(flow)

        assert latest_version(flow).published_at == first

    def test_a_broadcasts_own_flow_is_refused(self, tenancy):
        """Its sends name their version explicitly, so it would keep starting
        conversations; the broadcast has its own cancel."""
        from apps.broadcasts.models import Broadcast
        from apps.flows.tests.support import connection_for

        flow = _live(tenancy.workspace)
        Broadcast.objects.create(
            workspace=tenancy.workspace,
            name="Spring sale",
            channel_connection=connection_for(tenancy.workspace),
            flow=flow,
        )

        with pytest.raises(FlowNotLiveError):
            take_offline(flow)

        flow.refresh_from_db()
        assert flow.status == FlowStatus.ACTIVE
        assert published_version(flow) is not None

    def test_an_offline_flow_does_not_spend_a_plan_slot(self, tenancy):
        from apps.billing.entitlements import count_active_automations

        flow = _live(tenancy.workspace)
        before = count_active_automations(tenancy.workspace.organization)

        take_offline(flow)

        assert count_active_automations(tenancy.workspace.organization) == before - 1


@pytest.mark.django_db
class TestNothingInItCarriesOn:
    def test_a_conversation_waiting_in_the_flow_is_expired(self, tenancy):
        flow = _live(tenancy.workspace)
        execution = _waiting(contact_for(tenancy.workspace), flow)

        stopped = take_offline(flow)

        execution.refresh_from_db()
        assert stopped == 1
        assert execution.status == ExecutionStatus.EXPIRED
        assert execution.wait_config == {}

    def test_every_contact_in_it_is_stopped(self, tenancy):
        flow = _live(tenancy.workspace)
        runs = [_waiting(contact_for(tenancy.workspace, first_name=name), flow) for name in ("One", "Two", "Three")]

        assert take_offline(flow) == 3

        for run in runs:
            run.refresh_from_db()
            assert run.status == ExecutionStatus.EXPIRED

    def test_conversations_in_other_flows_are_untouched(self, tenancy):
        flow = _live(tenancy.workspace, name="Going offline")
        other = _live(tenancy.workspace, name="Staying live")
        theirs = _waiting(contact_for(tenancy.workspace, first_name="Other"), other)
        _waiting(contact_for(tenancy.workspace, first_name="Mine"), flow)

        take_offline(flow)

        theirs.refresh_from_db()
        assert theirs.status == ExecutionStatus.WAITING_REPLY

    def test_its_timers_are_cancelled_and_nobody_elses(self, tenancy):
        flow = _live(tenancy.workspace, name="Going offline")
        other = _live(tenancy.workspace, name="Staying live")
        mine = contact_for(tenancy.workspace, first_name="Mine")
        theirs = contact_for(tenancy.workspace, first_name="Theirs")
        execution = _waiting(mine, flow)
        followup = _timer(tenancy.workspace, mine, execution)
        resume = _timer(tenancy.workspace, mine, execution, ActionType.RESUME_EXECUTION)
        unrelated = _timer(tenancy.workspace, theirs, _waiting(theirs, other))

        take_offline(flow)

        for row in (followup, resume, unrelated):
            row.refresh_from_db()
        assert followup.status == ActionStatus.CANCELLED
        assert resume.status == ActionStatus.CANCELLED
        assert unrelated.status == ActionStatus.PENDING

    def test_a_preview_run_is_stopped_but_not_counted(self, tenancy):
        """The count is what the person is told about their contacts; their own
        "Test this flow" run is not one of them."""
        flow = _live(tenancy.workspace)
        customer = _waiting(contact_for(tenancy.workspace, first_name="Customer"), flow)
        with node_runtime("action", lambda ctx: Wait(WAIT_CONFIG)):
            test_run = start_flow(
                contact_for(tenancy.workspace, first_name="Editor"),
                flow,
                started_by=StartedBy.PREVIEW,
                flow_version=latest_version(flow),
                preview=True,
            )

        assert take_offline(flow) == 1

        for run in (customer, test_run):
            run.refresh_from_db()
            assert run.status == ExecutionStatus.EXPIRED

    def test_a_finished_conversation_is_not_rewritten(self, tenancy):
        flow = _live(tenancy.workspace)
        finished = start_flow(contact_for(tenancy.workspace), flow, started_by=StartedBy.API)
        assert finished.status == ExecutionStatus.COMPLETED

        assert take_offline(flow) == 0

        finished.refresh_from_db()
        assert finished.status == ExecutionStatus.COMPLETED

    def test_a_flow_set_live_again_in_between_keeps_its_conversations(self, tenancy, monkeypatch):
        """Somebody publishes it again between the two steps. What is running by
        the time the second step looks belongs to a live flow, so it is left be."""
        from apps.flows import services

        flow = _live(tenancy.workspace)
        execution = _waiting(contact_for(tenancy.workspace), flow)
        real_locked_status = services.locked_status

        def republished_first(target, *, lock):
            if lock == "share":
                publish(type(flow).objects.for_workspace(tenancy.workspace).get(pk=flow.pk))
            return real_locked_status(target, lock=lock)

        monkeypatch.setattr(services, "locked_status", republished_first)

        stopped = take_offline(flow)

        execution.refresh_from_db()
        assert stopped == 0
        assert execution.status == ExecutionStatus.WAITING_REPLY

    def test_nothing_running_is_a_no_op(self, tenancy):
        assert expire_flow_executions(_live(tenancy.workspace)) == 0

    def test_it_is_part_of_the_engines_public_surface(self):
        assert "expire_flow_executions" in engine.__all__


@pytest.mark.django_db
class TestNothingNewStartsIt:
    def test_starting_the_published_version_is_refused(self, tenancy):
        """The route every non-trigger entry point shares: the public API, a
        sequence step, another flow's start_flow, a queued start."""
        flow = _live(tenancy.workspace)
        take_offline(flow)

        with pytest.raises(FlowNotRunnableError):
            start_flow(contact_for(tenancy.workspace), flow, started_by=StartedBy.API)

    def test_a_queued_start_is_dropped_rather_than_run(self, tenancy):
        from apps.flows.handlers import handle_start_flow

        flow = _live(tenancy.workspace)
        contact = contact_for(tenancy.workspace)
        action = schedule(
            ActionType.START_FLOW,
            timezone.now(),
            {"contact_id": str(contact.pk), "flow_id": str(flow.pk)},
            workspace=tenancy.workspace,
            contact=contact,
        )
        take_offline(flow)

        handle_start_flow(action.payload, action)

        assert not flow.executions.exists()


@pytest.mark.django_db(transaction=True)
class TestAStartInFlight:
    """The race the share lock in ``_resolve_version`` closes.

    A start resolves the published version, then commits its execution a moment
    later. If the flow goes offline in between, the execution used to land after
    ``expire_flow_executions`` had looked, and carried on in an offline flow.
    """

    def test_going_offline_waits_for_it_and_then_stops_it(self, tenancy):
        from apps.flows.engine import runner

        flow = _live(tenancy.workspace)
        contact = contact_for(tenancy.workspace)
        resolved, proceed = threading.Event(), threading.Event()
        real_resolve = runner._resolve_version
        outcome: dict[str, object] = {}

        def resolve_then_pause(*args, **kwargs):
            version = real_resolve(*args, **kwargs)
            resolved.set()
            proceed.wait(timeout=10)
            return version

        def start() -> None:
            try:
                with node_runtime("action", lambda ctx: Wait(WAIT_CONFIG)):
                    outcome["execution"] = start_flow(contact, flow, started_by=StartedBy.API)
            except BaseException as exc:  # noqa: BLE001 - reported below
                outcome["start_error"] = exc
            finally:
                connection.close()

        def go_offline() -> None:
            try:
                outcome["stopped"] = take_offline(type(flow).objects.for_workspace(tenancy.workspace).get(pk=flow.pk))
            except BaseException as exc:  # noqa: BLE001 - reported below
                outcome["offline_error"] = exc
            finally:
                connection.close()

        runner._resolve_version = resolve_then_pause
        try:
            starter = threading.Thread(target=start)
            starter.start()
            assert resolved.wait(timeout=10)

            offliner = threading.Thread(target=go_offline)
            offliner.start()
            offliner.join(timeout=1)
            # Blocked on the start's share lock, not done.
            assert offliner.is_alive()

            proceed.set()
            starter.join(timeout=20)
            offliner.join(timeout=20)
        finally:
            runner._resolve_version = real_resolve
            proceed.set()

        assert "start_error" not in outcome, outcome
        assert "offline_error" not in outcome, outcome
        assert outcome["stopped"] == 1
        execution = outcome["execution"]
        execution.refresh_from_db()
        assert execution.status == ExecutionStatus.EXPIRED

    def test_a_start_after_it_sees_the_flow_offline(self, tenancy):
        """The caller's instance is stale — a trigger matched it before — and the
        status is re-read under the lock rather than trusted."""
        flow = _live(tenancy.workspace)
        stale = type(flow).objects.for_workspace(tenancy.workspace).get(pk=flow.pk)
        take_offline(flow)

        with pytest.raises(FlowNotRunnableError):
            start_flow(contact_for(tenancy.workspace), stale, started_by=StartedBy.API)
