"""The runner loop: dispatch, edge following, terminal states, and the events.

Everything here runs a real graph through :func:`apps.flows.engine.start_flow`
against the real database, because the loop's whole job is turning node results
into durable state — a test that stubbed the persistence would be testing the
half that never breaks.

Where a test is about a *result* rather than a *node*, it swaps the runtime with
:func:`~apps.flows.tests.support.node_runtime` and leaves the graph alone. The
graph has to stay publishable either way, and only two of the five results are
returned by a node this PR ships.
"""

from typing import Any

import pytest

from apps.contacts.errors import WorkspaceMismatchError
from apps.flows import events
from apps.flows.engine import Continue, End, Fail, FlowNotRunnableError, Wait, start_flow
from apps.flows.models import ExecutionStatus, FlowExecution, StartedBy
from apps.flows.services import archive_flow, create_flow, latest_version, save_draft
from apps.flows.tests.support import contact_for, edge, graph, node, node_runtime, published_flow

TAG_ACTION = {"actions": [{"verb": "add_tag", "tag": "seen"}]}
SECOND_TAG_ACTION = {"actions": [{"verb": "add_tag", "tag": "two"}]}
#: A verb that touches nothing: the workspace has no tag by this name, so
#: remove_tag is a clean no-op that needs neither the messaging facade nor a
#: fixture. Used wherever a test needs "some node" rather than a side effect.
NOOP_ACTION = {"actions": [{"verb": "remove_tag", "tag": "not-a-tag-here"}]}
EMPTY_CONDITION = {"match": "all", "rules": []}


def _tag_names(contact: Any) -> set[str]:
    return {tag.name for tag in contact.tags.all()}


@pytest.mark.django_db
class TestRunningAGraph:
    def test_a_single_action_node_runs_and_completes(self, tenancy):
        flow = published_flow(tenancy.workspace, graph([node("a", "action", TAG_ACTION)]))
        contact = contact_for(tenancy.workspace)

        execution = start_flow(contact, flow, started_by=StartedBy.API)

        assert execution.status == ExecutionStatus.COMPLETED
        assert execution.current_node_id == "a"
        assert _tag_names(contact) == {"seen"}

    def test_the_default_edge_is_followed(self, tenancy):
        document = graph(
            [node("a", "action", TAG_ACTION), node("b", "action", SECOND_TAG_ACTION, x=200)],
            [edge("a", "default", "b")],
        )
        flow = published_flow(tenancy.workspace, document)
        contact = contact_for(tenancy.workspace)

        execution = start_flow(contact, flow, started_by=StartedBy.API)

        assert execution.status == ExecutionStatus.COMPLETED
        assert execution.current_node_id == "b"
        assert _tag_names(contact) == {"seen", "two"}
        assert execution.blocks_since_pause == 2

    def test_a_missing_edge_ends_the_run(self, tenancy):
        """SPEC §9.2: "Missing edge for a handle -> End"."""
        flow = published_flow(tenancy.workspace, graph([node("a", "action", NOOP_ACTION)]))
        contact = contact_for(tenancy.workspace)

        execution = start_flow(contact, flow, started_by=StartedBy.API)

        assert execution.status == ExecutionStatus.COMPLETED

    def test_variables_are_seeded_and_persisted(self, tenancy):
        flow = published_flow(tenancy.workspace, graph([node("a", "action", NOOP_ACTION)]))
        contact = contact_for(tenancy.workspace)

        execution = start_flow(contact, flow, started_by=StartedBy.API, variables={"ref": "spring"})

        execution.refresh_from_db()
        assert execution.variables == {"ref": "spring"}

    def test_the_started_by_stamp_is_recorded(self, tenancy):
        flow = published_flow(tenancy.workspace, graph([node("a", "action", NOOP_ACTION)]))
        contact = contact_for(tenancy.workspace)

        execution = start_flow(contact, flow, started_by=StartedBy.stamp(StartedBy.TRIGGER, "0192-abc"))

        assert execution.started_by == "trigger:0192-abc"

    def test_the_channel_connection_is_remembered(self, tenancy):
        """Contract 1 needs one on every send; SPEC §9.3 routes replies by it."""
        from apps.channels.models import ChannelConnection

        connection = ChannelConnection.objects.create(
            workspace=tenancy.workspace, platform="telegram", display_name="Bot", external_id="1"
        )
        flow = published_flow(tenancy.workspace, graph([node("a", "action", NOOP_ACTION)]))
        contact = contact_for(tenancy.workspace)

        execution = start_flow(contact, flow, started_by=StartedBy.API, connection=connection)

        assert execution.channel_connection_id == connection.pk


@pytest.mark.django_db
class TestRefusals:
    def test_an_unpublished_flow_cannot_be_started(self, tenancy):
        flow = create_flow(workspace=tenancy.workspace, name="Draft only")
        save_draft(flow, graph([node("a", "action", NOOP_ACTION)]))
        contact = contact_for(tenancy.workspace)

        with pytest.raises(FlowNotRunnableError, match="no published version"):
            start_flow(contact, flow, started_by=StartedBy.API)

    def test_an_archived_flow_cannot_be_started(self, tenancy):
        flow = published_flow(tenancy.workspace, graph([node("a", "action", NOOP_ACTION)]))
        archive_flow(flow)
        contact = contact_for(tenancy.workspace)

        with pytest.raises(FlowNotRunnableError, match="archived"):
            start_flow(contact, flow, started_by=StartedBy.API)

    def test_a_contact_from_another_workspace_is_refused(self, tenancy, other_tenancy):
        flow = published_flow(tenancy.workspace, graph([node("a", "action", NOOP_ACTION)]))
        stranger = contact_for(other_tenancy.workspace)

        with pytest.raises(WorkspaceMismatchError):
            start_flow(stranger, flow, started_by=StartedBy.API)

    def test_a_connection_from_another_workspace_is_refused(self, tenancy, other_tenancy):
        """No model-level guard covers this FK, so the entry point has to.

        ``ContactScopedModel`` checks the contact against ``peer_field`` (the
        flow) and nothing else. A foreign connection stored here would be handed
        straight to ``send_outbound`` on the first send, putting this
        workspace's contact on another tenant's channel.
        """
        from apps.channels.models import ChannelConnection

        theirs = ChannelConnection.objects.create(
            workspace=other_tenancy.workspace, platform="telegram", display_name="Theirs", external_id="tg-theirs"
        )
        flow = published_flow(tenancy.workspace, graph([node("a", "action", NOOP_ACTION)]))
        contact = contact_for(tenancy.workspace)

        with pytest.raises(WorkspaceMismatchError, match="channel connection"):
            start_flow(contact, flow, started_by=StartedBy.API, connection=theirs)

        assert not FlowExecution.objects.for_workspace(tenancy.workspace).exists()

    def test_a_version_of_another_flow_is_a_programming_error(self, tenancy):
        flow = published_flow(tenancy.workspace, graph([node("a", "action", NOOP_ACTION)]))
        other = published_flow(tenancy.workspace, graph([node("a", "action", NOOP_ACTION)]), name="Other")
        contact = contact_for(tenancy.workspace)

        with pytest.raises(ValueError, match="different flow"):
            start_flow(contact, flow, started_by=StartedBy.API, flow_version=latest_version(other))

    def test_a_graph_with_no_entry_node_cannot_start(self, tenancy):
        """Publishing rejects this; a draft preview is how it reaches the runner.

        Two nodes pointing at each other, not one node pointing at itself: a
        self-edge is deliberately *not* an incoming edge (see
        ``validation._entry_nodes``), because a question that re-asks itself on
        timeout is the commonest retry shape there is.
        """
        flow = published_flow(tenancy.workspace, graph([node("a", "action", NOOP_ACTION)]))
        broken = save_draft(
            flow,
            graph(
                [node("a", "action", NOOP_ACTION), node("b", "action", NOOP_ACTION, x=200)],
                [edge("a", "default", "b"), edge("b", "default", "a")],
            ),
        )
        contact = contact_for(tenancy.workspace)

        with pytest.raises(FlowNotRunnableError, match="no single entry node"):
            start_flow(contact, flow, started_by=StartedBy.PREVIEW, flow_version=broken)


@pytest.mark.django_db
class TestBrokenGraphs:
    def test_a_node_type_with_no_runtime_fails_the_run(self, tenancy):
        """``send_sms`` has a schema (L2-D) and no runtime until L5-D."""
        flow = published_flow(tenancy.workspace, graph([node("a", "send_sms", {"text": "hi"})]))
        contact = contact_for(tenancy.workspace)

        execution = start_flow(contact, flow, started_by=StartedBy.API)

        assert execution.status == ExecutionStatus.FAILED
        assert "no runtime is registered for 'send_sms'" in execution.last_error

    def test_an_edge_to_a_vanished_node_fails_the_run(self, tenancy):
        """Only reachable on a draft, which autosave rewrites under a preview."""
        flow = published_flow(tenancy.workspace, graph([node("a", "action", NOOP_ACTION)]))
        version = save_draft(
            flow,
            graph(
                [node("a", "action", NOOP_ACTION)],
                [{"id": "e1", "source": "a", "sourceHandle": "default", "target": "ghost"}],
            ),
        )
        contact = contact_for(tenancy.workspace)

        execution = start_flow(contact, flow, started_by=StartedBy.PREVIEW, flow_version=version)

        assert execution.status == ExecutionStatus.FAILED
        assert "'ghost' is not in this flow version's graph" in execution.last_error

    def test_a_node_raising_propagates_so_the_queue_can_retry(self, tenancy):
        """Unknown exceptions are not swallowed — see the runner's docstring.

        Catching them would commit the half-finished writes of the node that
        blew up, in the same transaction, and call the run cleanly dead.
        """
        flow = published_flow(tenancy.workspace, graph([node("a", "action", NOOP_ACTION)]))
        contact = contact_for(tenancy.workspace)

        def _explode(_ctx):
            raise RuntimeError("kaboom")

        with node_runtime("action", _explode), pytest.raises(RuntimeError, match="kaboom"):
            start_flow(contact, flow, started_by=StartedBy.API)

        assert not FlowExecution.objects.for_workspace(tenancy.workspace).exists()


@pytest.mark.django_db
class TestStepResults:
    """Each of the five, and what the runner writes for it."""

    def test_continue_follows_the_named_handle(self, tenancy):
        document = graph(
            [node("a", "condition", EMPTY_CONDITION), node("b", "action", TAG_ACTION, x=200)],
            [edge("a", "cond:true", "b")],
        )
        flow = published_flow(tenancy.workspace, document)
        contact = contact_for(tenancy.workspace)

        with node_runtime("condition", lambda ctx: Continue("cond:true")):
            execution = start_flow(contact, flow, started_by=StartedBy.API)

        assert execution.current_node_id == "b"
        assert _tag_names(contact) == {"seen"}

    def test_wait_parks_the_execution_and_resets_the_counter(self, tenancy):
        document = graph(
            [node("a", "action", TAG_ACTION), node("b", "action", NOOP_ACTION, x=200)],
            [edge("a", "default", "b")],
        )
        flow = published_flow(tenancy.workspace, document)
        contact = contact_for(tenancy.workspace)
        config = {"type": "buttons", "token": "t1", "handles": {"yes": "btn:yes"}}

        with node_runtime("action", lambda ctx: Wait(config) if ctx.node_id == "b" else Continue()):
            execution = start_flow(contact, flow, started_by=StartedBy.API)

        assert execution.status == ExecutionStatus.WAITING_REPLY
        assert execution.current_node_id == "b"
        assert execution.blocks_since_pause == 0
        assert execution.wait_config == config

    def test_every_wait_is_given_a_token(self, tenancy):
        """The staleness guard has to hold for waits a node forgot to tokenise."""
        flow = published_flow(tenancy.workspace, graph([node("a", "action", NOOP_ACTION)]))
        contact = contact_for(tenancy.workspace)

        with node_runtime("action", lambda ctx: Wait({"type": "buttons", "handles": {}})):
            execution = start_flow(contact, flow, started_by=StartedBy.API)

        assert execution.wait_config["token"]

    def test_a_wait_carrying_a_deadline_arms_a_followup_timer(self, tenancy):
        """SPEC §11.1's ``followup``. The node asks; the runner writes the row."""
        from django.utils import timezone

        from apps.queueing.models import ActionType, ScheduledAction

        flow = published_flow(tenancy.workspace, graph([node("a", "action", NOOP_ACTION)]))
        contact = contact_for(tenancy.workspace)
        deadline = timezone.now() + timezone.timedelta(hours=1)
        config = {
            "type": "buttons",
            "token": "t1",
            "handles": {},
            "timeout": {"handle": "timeout", "run_at": deadline.isoformat()},
        }

        with node_runtime("action", lambda ctx: Wait(config)):
            execution = start_flow(contact, flow, started_by=StartedBy.API)

        timer = ScheduledAction.objects.for_workspace(tenancy.workspace).get(type=ActionType.FOLLOWUP_TIMER)
        assert timer.run_at == deadline
        assert timer.payload == {"execution_id": str(execution.pk), "handle": "timeout", "token": "t1"}

    def test_a_wait_with_no_deadline_arms_nothing(self, tenancy):
        from apps.queueing.models import ActionType, ScheduledAction

        flow = published_flow(tenancy.workspace, graph([node("a", "action", NOOP_ACTION)]))
        contact = contact_for(tenancy.workspace)

        with node_runtime("action", lambda ctx: Wait({"type": "buttons", "token": "t1", "handles": {}})):
            start_flow(contact, flow, started_by=StartedBy.API)

        assert (
            not ScheduledAction.objects.for_workspace(tenancy.workspace).filter(type=ActionType.FOLLOWUP_TIMER).exists()
        )

    def test_schedule_parks_the_execution_and_enqueues_its_own_wake_up(self, tenancy):
        from django.utils import timezone

        from apps.flows.engine.results import Schedule
        from apps.queueing.models import ActionType, ScheduledAction

        flow = published_flow(tenancy.workspace, graph([node("a", "action", NOOP_ACTION)]))
        contact = contact_for(tenancy.workspace)
        run_at = timezone.now() + timezone.timedelta(hours=2)

        with node_runtime("action", lambda ctx: Schedule(run_at, config={"token": "d1"})):
            execution = start_flow(contact, flow, started_by=StartedBy.API)

        assert execution.status == ExecutionStatus.WAITING_DELAY
        assert execution.blocks_since_pause == 0
        assert execution.wait_config == {"type": "smart_delay", "token": "d1", "handle": "default"}

        action = ScheduledAction.objects.for_workspace(tenancy.workspace).get(type=ActionType.RESUME_EXECUTION)
        assert action.run_at == run_at
        assert action.payload == {"execution_id": str(execution.pk), "handle": "default", "token": "d1"}
        assert action.contact_id == contact.pk

    def test_fail_records_the_reason(self, tenancy):
        flow = published_flow(tenancy.workspace, graph([node("a", "action", NOOP_ACTION)]))
        contact = contact_for(tenancy.workspace)

        with node_runtime("action", lambda ctx: Fail("nope")):
            execution = start_flow(contact, flow, started_by=StartedBy.API)

        assert execution.status == ExecutionStatus.FAILED
        assert execution.last_error == "nope"
        assert execution.wait_config == {}

    def test_end_completes_without_following_an_edge(self, tenancy):
        document = graph(
            [node("a", "action", NOOP_ACTION), node("b", "action", TAG_ACTION, x=200)],
            [edge("a", "default", "b")],
        )
        flow = published_flow(tenancy.workspace, document)
        contact = contact_for(tenancy.workspace)

        with node_runtime("action", lambda ctx: End()):
            execution = start_flow(contact, flow, started_by=StartedBy.API)

        assert execution.status == ExecutionStatus.COMPLETED
        assert execution.current_node_id == "a"
        assert _tag_names(contact) == set()


@pytest.mark.django_db
class TestCompletionEvent:
    def test_execution_completed_carries_ids_only(self, tenancy):
        received: list[dict[str, Any]] = []

        def _receiver(sender, **kwargs):
            received.append(kwargs)

        events.execution_completed.connect(_receiver)
        try:
            flow = published_flow(tenancy.workspace, graph([node("a", "action", NOOP_ACTION)]))
            contact = contact_for(tenancy.workspace)
            execution = start_flow(contact, flow, started_by=StartedBy.API)
        finally:
            events.execution_completed.disconnect(_receiver)

        assert len(received) == 1
        payload = received[0]
        assert payload["event"] == "execution.completed"
        assert payload["execution_id"] == execution.pk
        assert payload["flow_id"] == flow.pk
        assert payload["contact_id"] == contact.pk
        assert payload["workspace_id"] == tenancy.workspace.pk
        assert payload["preview"] is False
        # Ids and flags only — contract 7 forbids names and bodies.
        assert not any(key in payload for key in ("flow_name", "variables", "text"))

    def test_a_failed_execution_does_not_emit(self, tenancy):
        received: list[dict[str, Any]] = []

        def _receiver(sender, **kwargs):
            received.append(kwargs)

        events.execution_completed.connect(_receiver)
        try:
            flow = published_flow(tenancy.workspace, graph([node("a", "send_sms", {"text": "hi"})]))
            contact = contact_for(tenancy.workspace)
            start_flow(contact, flow, started_by=StartedBy.API)
        finally:
            events.execution_completed.disconnect(_receiver)

        assert received == []
