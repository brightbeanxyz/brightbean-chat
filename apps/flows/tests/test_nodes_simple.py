"""The PR-1 node set: action (and its verbs), condition, randomizer, start_flow, note.

Each node is exercised through the runner rather than by calling ``execute``
directly. A node's output is a ``StepResult``, but its *meaning* is the edge the
runner follows and the row it writes, and a node that returned the right result
against the wrong handle name would pass a unit test and route nobody.
"""

from decimal import Decimal
from typing import Any

import pytest

from apps.contacts.services import create_custom_field, field_values_for, get_or_create_tag
from apps.flows.engine import start_flow
from apps.flows.models import ExecutionStatus, StartedBy
from apps.flows.services import save_draft
from apps.flows.tests.support import FakeFacade, contact_for, edge, graph, node, published_flow
from apps.notifications.models import Notification


def _tags(contact: Any) -> set[str]:
    return {tag.name for tag in contact.tags.all()}


def _action(*steps: dict[str, Any]) -> dict[str, Any]:
    return {"actions": list(steps)}


@pytest.mark.django_db
class TestActionVerbsOnContacts:
    def test_add_tag_creates_the_tag_when_the_workspace_has_none(self, tenancy):
        """A flow is often what introduces a tag; refusing would be unhelpful."""
        flow = published_flow(
            tenancy.workspace, graph([node("a", "action", _action({"verb": "add_tag", "tag": "VIP"}))])
        )
        contact = contact_for(tenancy.workspace)

        start_flow(contact, flow, started_by=StartedBy.API)

        assert _tags(contact) == {"VIP"}

    def test_add_tag_reuses_an_existing_tag_case_insensitively(self, tenancy):
        existing, _ = get_or_create_tag(tenancy.workspace, "Lead")
        flow = published_flow(
            tenancy.workspace, graph([node("a", "action", _action({"verb": "add_tag", "tag": "lead"}))])
        )
        contact = contact_for(tenancy.workspace)

        start_flow(contact, flow, started_by=StartedBy.API)

        assert list(contact.tags.values_list("pk", flat=True)) == [existing.pk]

    def test_remove_tag_is_a_no_op_when_the_tag_does_not_exist(self, tenancy):
        flow = published_flow(
            tenancy.workspace, graph([node("a", "action", _action({"verb": "remove_tag", "tag": "ghost"}))])
        )
        contact = contact_for(tenancy.workspace)

        execution = start_flow(contact, flow, started_by=StartedBy.API)

        assert execution.status == ExecutionStatus.COMPLETED

    def test_remove_tag_untags(self, tenancy):
        from apps.contacts.services import add_tag

        tag, _ = get_or_create_tag(tenancy.workspace, "Lead")
        contact = contact_for(tenancy.workspace)
        add_tag(contact, tag)
        flow = published_flow(
            tenancy.workspace, graph([node("a", "action", _action({"verb": "remove_tag", "tag": "LEAD"}))])
        )

        start_flow(contact, flow, started_by=StartedBy.API)

        assert _tags(contact) == set()

    def test_set_field_renders_placeholders_in_the_value(self, tenancy):
        field = create_custom_field(tenancy.workspace, name="Greeting", field_type="text")
        config = _action({"verb": "set_field", "field": "greeting", "value": "Hi {{first_name}}"})
        flow = published_flow(tenancy.workspace, graph([node("a", "action", config)]))
        contact = contact_for(tenancy.workspace, first_name="Ada")

        start_flow(contact, flow, started_by=StartedBy.API)

        assert field_values_for(contact)[field.pk] == "Hi Ada"

    def test_set_field_parses_a_number(self, tenancy):
        field = create_custom_field(tenancy.workspace, name="Score", field_type="number")
        config = _action({"verb": "set_field", "field": "Score", "value": "42.5"})
        flow = published_flow(tenancy.workspace, graph([node("a", "action", config)]))
        contact = contact_for(tenancy.workspace)

        start_flow(contact, flow, started_by=StartedBy.API)

        assert field_values_for(contact)[field.pk] == Decimal("42.5")

    @pytest.mark.parametrize(("text", "expected"), [("true", True), ("Yes", True), ("no", False), ("0", False)])
    def test_set_field_reads_boolean_words(self, tenancy, text, expected):
        """A flow's value is always a string; ``coerce_value`` refuses one for a bool."""
        field = create_custom_field(tenancy.workspace, name="Opted", field_type="boolean")
        config = _action({"verb": "set_field", "field": "opted", "value": text})
        flow = published_flow(tenancy.workspace, graph([node("a", "action", config)]))
        contact = contact_for(tenancy.workspace)

        start_flow(contact, flow, started_by=StartedBy.API)

        assert field_values_for(contact)[field.pk] is expected

    def test_a_bad_value_is_logged_and_the_flow_continues(self, tenancy, caplog):
        """SPEC §11.2: the action node "always continues"."""
        create_custom_field(tenancy.workspace, name="Score", field_type="number")
        config = _action(
            {"verb": "set_field", "field": "Score", "value": "not a number"},
            {"verb": "add_tag", "tag": "still-ran"},
        )
        flow = published_flow(tenancy.workspace, graph([node("a", "action", config)]))
        contact = contact_for(tenancy.workspace)

        execution = start_flow(contact, flow, started_by=StartedBy.API)

        assert execution.status == ExecutionStatus.COMPLETED
        assert _tags(contact) == {"still-ran"}

    def test_clear_field_removes_the_value(self, tenancy):
        from apps.contacts.services import set_field_value

        field = create_custom_field(tenancy.workspace, name="Greeting", field_type="text")
        contact = contact_for(tenancy.workspace)
        set_field_value(contact, field, "hello")
        config = _action({"verb": "clear_field", "field": "greeting"})
        flow = published_flow(tenancy.workspace, graph([node("a", "action", config)]))

        start_flow(contact, flow, started_by=StartedBy.API)

        assert field_values_for(contact) == {}

    def test_a_field_the_workspace_does_not_have_is_logged(self, tenancy, caplog):
        config = _action({"verb": "set_field", "field": "nope", "value": "x"})
        flow = published_flow(tenancy.workspace, graph([node("a", "action", config)]))
        contact = contact_for(tenancy.workspace)

        with caplog.at_level("WARNING"):
            execution = start_flow(contact, flow, started_by=StartedBy.API)

        assert execution.status == ExecutionStatus.COMPLETED
        assert "which this workspace does not have" in caplog.text


@pytest.mark.django_db
class TestActionVerbsOnMessagingAndMembers:
    def test_conversation_verbs_go_through_the_facade(self, tenancy, monkeypatch):
        facade = FakeFacade().install(monkeypatch)
        config = _action({"verb": "open_conversation"}, {"verb": "close_conversation"})
        flow = published_flow(tenancy.workspace, graph([node("a", "action", config)]))
        contact = contact_for(tenancy.workspace)

        start_flow(contact, flow, started_by=StartedBy.API)

        assert [name for name, _ in facade.calls] == ["open_conversation", "close_conversation"]

    def test_assign_conversation_resolves_a_member_of_this_workspace(self, tenancy, monkeypatch):
        facade = FakeFacade().install(monkeypatch)
        agent = tenancy.user_for("agent")
        config = _action({"verb": "assign_conversation", "member": str(agent.pk)})
        flow = published_flow(tenancy.workspace, graph([node("a", "action", config)]))
        contact = contact_for(tenancy.workspace)

        start_flow(contact, flow, started_by=StartedBy.API)

        assigned = facade.named("assign_conversation")
        assert assigned and assigned[0]["args"][1] == agent

    def test_a_member_from_another_workspace_is_refused(self, tenancy, other_tenancy, monkeypatch, caplog):
        """A graph is editable by anyone with edit_flows; the id is not trusted."""
        facade = FakeFacade().install(monkeypatch)
        stranger = other_tenancy.owner
        config = _action({"verb": "assign_conversation", "member": str(stranger.pk)})
        flow = published_flow(tenancy.workspace, graph([node("a", "action", config)]))
        contact = contact_for(tenancy.workspace)

        with caplog.at_level("WARNING"):
            start_flow(contact, flow, started_by=StartedBy.API)

        assert facade.named("assign_conversation") == []
        assert "is not in this workspace" in caplog.text

    def test_notify_members_uses_the_registered_copy(self, tenancy):
        editor = tenancy.user_for("editor")
        config = _action(
            {
                "verb": "notify_members",
                "member_ids": [str(editor.pk)],
                "via": "in_app",
                "text": "New lead: {{first_name}}",
            }
        )
        flow = published_flow(tenancy.workspace, graph([node("a", "action", config)]), name="Lead capture")
        contact = contact_for(tenancy.workspace, first_name="Ada")

        start_flow(contact, flow, started_by=StartedBy.API)

        notification = Notification.objects.get(user=editor)
        assert notification.event_type == "member_mentioned_in_app"
        assert "Lead capture" in notification.title
        assert notification.body == "New lead: Ada"

    def test_via_email_uses_the_emailing_event(self, tenancy):
        editor = tenancy.user_for("editor")
        config = _action({"verb": "notify_members", "member_ids": [str(editor.pk)], "via": "email", "text": "Ping"})
        flow = published_flow(tenancy.workspace, graph([node("a", "action", config)]))
        contact = contact_for(tenancy.workspace)

        start_flow(contact, flow, started_by=StartedBy.API)

        assert Notification.objects.get(user=editor).event_type == "member_mentioned"


@pytest.mark.django_db
class TestAVerbWithNoRuntime:
    """A declared verb whose owner has not landed logs and moves on.

    Every verb in ``ACTION_VERBS`` has a runtime now that L6-A has registered the
    sequence pair, so this simulates the state by removing one — which is what
    ``engine.registry.unregister_verb`` exists for. The path itself is not
    hypothetical: it is what ``apps/flows/engine/nodes/action.py`` promises for
    the next verb that ships a schema before its behaviour.
    """

    def test_it_warns_and_the_rest_of_the_node_still_runs(self, tenancy, caplog):
        from apps.flows.engine.registry import register_verb, unregister_verb, verb_handler

        restore = verb_handler("subscribe_sequence")
        unregister_verb("subscribe_sequence")
        try:
            config = _action(
                {"verb": "subscribe_sequence", "sequence": "0192f000-0000-7000-8000-0000000000a1"},
                {"verb": "add_tag", "tag": "still-ran"},
            )
            flow = published_flow(tenancy.workspace, graph([node("a", "action", config)]))
            contact = contact_for(tenancy.workspace)

            with caplog.at_level("WARNING"):
                execution = start_flow(contact, flow, started_by=StartedBy.API)
        finally:
            if restore is not None:
                register_verb("subscribe_sequence", restore, replace=True)

        assert execution.status == ExecutionStatus.COMPLETED
        assert "has no runtime in this deployment" in caplog.text
        assert _tags(contact) == {"still-ran"}


@pytest.mark.django_db
class TestConditionNode:
    def _branching_flow(self, workspace, rules):
        document = graph(
            [
                node("c", "condition", {"match": "all", "rules": rules}),
                node("yes", "action", _action({"verb": "add_tag", "tag": "yes"}), x=200),
                node("no", "action", _action({"verb": "add_tag", "tag": "no"}), x=400),
            ],
            [edge("c", "cond:true", "yes"), edge("c", "cond:false", "no")],
        )
        return published_flow(workspace, document)

    def test_a_matching_contact_takes_the_true_branch(self, tenancy):
        from apps.contacts.services import add_tag

        tag, _ = get_or_create_tag(tenancy.workspace, "VIP")
        contact = contact_for(tenancy.workspace)
        add_tag(contact, tag)
        flow = self._branching_flow(tenancy.workspace, [{"source": "tag", "key": str(tag.pk), "op": "has"}])

        execution = start_flow(contact, flow, started_by=StartedBy.API)

        assert execution.current_node_id == "yes"
        assert "yes" in _tags(contact)

    def test_a_non_matching_contact_takes_the_false_branch(self, tenancy):
        tag, _ = get_or_create_tag(tenancy.workspace, "VIP")
        contact = contact_for(tenancy.workspace)
        flow = self._branching_flow(tenancy.workspace, [{"source": "tag", "key": str(tag.pk), "op": "has"}])

        execution = start_flow(contact, flow, started_by=StartedBy.API)

        assert execution.current_node_id == "no"

    def test_a_filter_naming_a_deleted_object_fails_the_run(self, tenancy):
        """Diagnosable and permanent, so it is a Fail rather than a retry."""
        tag, _ = get_or_create_tag(tenancy.workspace, "VIP")
        flow = self._branching_flow(tenancy.workspace, [{"source": "tag", "key": str(tag.pk), "op": "has"}])
        tag.delete()
        contact = contact_for(tenancy.workspace)

        execution = start_flow(contact, flow, started_by=StartedBy.API)

        assert execution.status == ExecutionStatus.FAILED
        assert "condition node c" in execution.last_error

    def test_an_unregistered_condition_source_fails_the_run(self, tenancy):
        """A source declared with no ``build_q`` validates at publish and fails at runtime.

        Every declared source has an implementation now — ``window`` came with
        L3-A and ``sequence`` with L6-A — so the slot state is staged rather than
        found. The behaviour under test is the contract itself: a filter using an
        unimplemented source is storable and publishable, and raises by name when
        a node actually runs it.
        """
        from apps.campaigns.models import Sequence
        from apps.contacts.conditions import ConditionSource, register_source, sources

        sequence = Sequence.objects.create(workspace=tenancy.workspace, name="Onboarding")
        implemented = sources()["sequence"]
        slot = ConditionSource(
            implemented.name, implemented.label, implemented.key_kind, implemented.ops, None, implemented.owner
        )
        register_source(slot, replace=True)
        try:
            flow = self._branching_flow(
                tenancy.workspace, [{"source": "sequence", "key": str(sequence.pk), "op": "subscribed"}]
            )
            contact = contact_for(tenancy.workspace)

            execution = start_flow(contact, flow, started_by=StartedBy.API)
        finally:
            register_source(implemented, replace=True)

        assert execution.status == ExecutionStatus.FAILED


@pytest.mark.django_db
class TestRandomizerNode:
    def _split_flow(self, workspace, paths, sticky=True):
        document = graph(
            [
                node("r", "randomizer", {"paths": paths, "sticky": sticky}),
                node("a", "action", _action({"verb": "add_tag", "tag": "a"}), x=200),
                node("b", "action", _action({"verb": "add_tag", "tag": "b"}), x=400),
            ],
            [edge("r", "rand:a", "a"), edge("r", "rand:b", "b")],
        )
        return published_flow(workspace, document)

    def test_a_hundred_percent_weight_always_wins(self, tenancy):
        flow = self._split_flow(tenancy.workspace, [{"id": "a", "weight": 100}, {"id": "b", "weight": 0}])
        for index in range(10):
            contact = contact_for(tenancy.workspace, first_name=f"C{index}")
            execution = start_flow(contact, flow, started_by=StartedBy.API)
            assert execution.current_node_id == "a"

    def test_sticky_remembers_the_arm_across_runs(self, tenancy):
        """What makes an A/B test a comparison of two populations."""
        flow = self._split_flow(tenancy.workspace, [{"id": "a", "weight": 50}, {"id": "b", "weight": 50}])
        contact = contact_for(tenancy.workspace)

        first = start_flow(contact, flow, started_by=StartedBy.API)
        chosen = first.variables["rand:r"]

        for _ in range(10):
            again = start_flow(contact, flow, started_by=StartedBy.API, variables=dict(first.variables))
            assert again.variables["rand:r"] == chosen
            assert again.current_node_id == chosen

    def test_non_sticky_records_nothing(self, tenancy):
        flow = self._split_flow(tenancy.workspace, [{"id": "a", "weight": 50}, {"id": "b", "weight": 50}], sticky=False)
        contact = contact_for(tenancy.workspace)

        execution = start_flow(contact, flow, started_by=StartedBy.API)

        assert "rand:r" not in execution.variables

    def test_all_zero_weights_still_route(self, tenancy):
        """Two clicks in the builder produce this; stranding the flow would be worse."""
        flow = self._split_flow(tenancy.workspace, [{"id": "a", "weight": 0}, {"id": "b", "weight": 0}])
        contact = contact_for(tenancy.workspace)

        execution = start_flow(contact, flow, started_by=StartedBy.API)

        assert execution.current_node_id in {"a", "b"}

    def test_weights_split_roughly_in_proportion(self, tenancy):
        """A smoke test on the sampling, not a statistics exam."""
        flow = self._split_flow(tenancy.workspace, [{"id": "a", "weight": 90}, {"id": "b", "weight": 10}])
        landed = []
        for index in range(120):
            contact = contact_for(tenancy.workspace, first_name=f"C{index}")
            landed.append(start_flow(contact, flow, started_by=StartedBy.API).current_node_id)

        assert 60 < landed.count("a") < 120


@pytest.mark.django_db
class TestStartFlowNode:
    def test_it_completes_this_run_and_starts_the_target(self, tenancy):
        target = published_flow(
            tenancy.workspace,
            graph([node("t", "action", _action({"verb": "add_tag", "tag": "arrived"}))]),
            name="Target",
        )
        source = published_flow(
            tenancy.workspace, graph([node("s", "start_flow", {"flow_id": str(target.pk)})]), name="Source"
        )
        contact = contact_for(tenancy.workspace)

        execution = start_flow(contact, source, started_by=StartedBy.API)

        assert execution.flow_id == target.pk
        assert execution.status == ExecutionStatus.COMPLETED
        assert execution.started_by.startswith("flow:")
        assert _tags(contact) == {"arrived"}

    def test_the_source_execution_is_completed_not_expired(self, tenancy):
        """SPEC §11.3 says "terminates current execution (completed)"."""
        from apps.flows.models import FlowExecution

        target = published_flow(
            tenancy.workspace, graph([node("t", "action", _action({"verb": "add_tag", "tag": "x"}))]), name="T"
        )
        source = published_flow(
            tenancy.workspace, graph([node("s", "start_flow", {"flow_id": str(target.pk)})]), name="S"
        )
        contact = contact_for(tenancy.workspace)

        start_flow(contact, source, started_by=StartedBy.API)

        first = FlowExecution.objects.for_workspace(tenancy.workspace).get(flow=source)
        assert first.status == ExecutionStatus.COMPLETED

    def test_variables_are_carried_across(self, tenancy):
        target = published_flow(
            tenancy.workspace, graph([node("t", "action", _action({"verb": "remove_tag", "tag": "none"}))]), name="T"
        )
        source = published_flow(
            tenancy.workspace, graph([node("s", "start_flow", {"flow_id": str(target.pk)})]), name="S"
        )
        contact = contact_for(tenancy.workspace)

        execution = start_flow(contact, source, started_by=StartedBy.API, variables={"ref": "spring"})

        assert execution.variables == {"ref": "spring"}

    def test_a_missing_target_is_a_warning_and_the_run_still_completed(self, tenancy, caplog):
        """The graph ran to its end; the *next* flow is missing, not this one."""
        ghost = "0192f000-0000-7000-8000-0000000000ff"
        source = published_flow(tenancy.workspace, graph([node("s", "start_flow", {"flow_id": ghost})]))
        contact = contact_for(tenancy.workspace)

        with caplog.at_level("WARNING"):
            execution = start_flow(contact, source, started_by=StartedBy.API)

        assert execution.status == ExecutionStatus.COMPLETED
        assert "does not exist here" in caplog.text

    def test_a_target_in_another_workspace_is_not_reachable(self, tenancy, other_tenancy, caplog):
        theirs = published_flow(
            other_tenancy.workspace, graph([node("t", "action", _action({"verb": "add_tag", "tag": "leak"}))])
        )
        source = published_flow(tenancy.workspace, graph([node("s", "start_flow", {"flow_id": str(theirs.pk)})]))
        contact = contact_for(tenancy.workspace)

        with caplog.at_level("WARNING"):
            execution = start_flow(contact, source, started_by=StartedBy.API)

        assert execution.status == ExecutionStatus.COMPLETED
        assert _tags(contact) == set()

    def test_an_unpublished_target_is_a_warning(self, tenancy, caplog):
        from apps.flows.services import create_flow

        target = create_flow(workspace=tenancy.workspace, name="Never published")
        save_draft(target, graph([node("t", "action", _action({"verb": "add_tag", "tag": "x"}))]))
        source = published_flow(tenancy.workspace, graph([node("s", "start_flow", {"flow_id": str(target.pk)})]))
        contact = contact_for(tenancy.workspace)

        with caplog.at_level("WARNING"):
            execution = start_flow(contact, source, started_by=StartedBy.API)

        assert execution.status == ExecutionStatus.COMPLETED
        assert "could not hand off" in caplog.text


@pytest.mark.django_db
class TestNoteNode:
    def test_a_note_takes_no_part_in_routing(self, tenancy):
        """Validation forbids an edge to a note, so it can only sit beside one."""
        document = graph(
            [node("a", "action", _action({"verb": "add_tag", "tag": "ran"})), node("n", "note", {"text": "hi"}, x=200)]
        )
        flow = published_flow(tenancy.workspace, document)
        contact = contact_for(tenancy.workspace)

        execution = start_flow(contact, flow, started_by=StartedBy.API)

        assert execution.current_node_id == "a"
        assert _tags(contact) == {"ran"}
