"""The rules hook at contract 6's ``post_persist`` stage.

Issue #24's first acceptance criterion, in one file: "Hook-stage ordering
verified: rules run post-persist, pre-trigger; cumulative application in priority
order; disabled rules skipped."

Everything here is driven through the **real** pipeline rather than by calling
the hook — ``route_events`` for the inline path and ``handle_route_event`` for
the worker replay. That duplication is the point. ``post_persist`` runs outside
any transaction inline and inside one holding the contact advisory lock on the
worker, so a test that only drove one of them would be testing half the code
under half the failure semantics.
"""

from typing import Any

import pytest
from django.utils import timezone

from apps.channels.events import EventType
from apps.flows.tests.routing_support import routing_adapter
from apps.flows.tests.support import inbound as _raw_event
from apps.flows.triggers.handlers import ROUTE_EVENT, handle_route_event
from apps.flows.triggers.hooks import Stage, register_hook, unregister_hook
from apps.flows.triggers.pipeline import route_events
from apps.flows.triggers.serialization import event_to_payload
from apps.inbox.models import (
    ConversationLabel,
    ConversationLabelLink,
    InboxRule,
    InboxRuleApplication,
)
from apps.inbox.routing import apply_inbox_rules
from apps.messaging.models import Conversation, ConversationState
from apps.queueing.models import ScheduledAction

pytestmark = pytest.mark.django_db


#: The conftest already binds ``inbound`` to a *message* factory, and this file
#: needs the *event* one. Renamed rather than shadowed: two things called
#: ``inbound``, one of which silently wins, is how these tests all passed against
#: a hook that had never run.
def event(connection: Any, **kwargs: Any) -> Any:
    """One inbound ``NormalizedEvent`` addressed to the identity fixture."""
    kwargs.setdefault("user", "u1")
    return _raw_event(connection, **kwargs)


def _rule(workspace: Any, *, condition: dict, actions: list, **overrides: Any) -> InboxRule:
    values: dict[str, Any] = {"name": "Rule", "enabled": True, "priority": 0}
    values.update(overrides)
    rule = InboxRule(workspace=workspace, condition_json=condition, actions_json=actions, **values)
    rule.save()
    return rule


def _label(workspace: Any, name: str = "Refunds") -> ConversationLabel:
    return ConversationLabel.objects.create(workspace=workspace, name=name)


def _deliver(connection: Any, event: Any) -> None:
    """One event through the whole inline pipeline, as a webhook would."""
    with routing_adapter(connection.platform):
        route_events(connection, [event])


def _replay(workspace: Any, connection: Any, event: Any, *, stage: str = "post_persist") -> None:
    """The same event through the worker, from a named stage."""
    action = ScheduledAction.objects.create(
        workspace=workspace,
        type=ROUTE_EVENT,
        run_at=timezone.now(),
        payload={"stage": stage, "connection_id": str(connection.pk), "event": event_to_payload(event)},
    )
    with routing_adapter(connection.platform):
        handle_route_event(action.payload, action)


def _labels_on(conversation: Conversation) -> list[str]:
    return sorted(
        link.label.name
        for link in ConversationLabelLink.objects.for_workspace(conversation.workspace_id)
        .filter(conversation=conversation)
        .select_related("label")
    )


class TestWhatFires:
    def test_a_matching_rule_labels_the_thread(self, tenancy, connection, identity, conversation):
        _rule(
            tenancy.workspace,
            condition={"keywords": [{"text": "refund", "mode": "contains"}]},
            actions=[{"type": "add_label", "label_id": str(_label(tenancy.workspace).pk)}],
        )

        _deliver(connection, event(connection, text="i need a refund"))

        assert _labels_on(conversation) == ["Refunds"]

    def test_a_rule_that_does_not_match_does_nothing(self, tenancy, connection, identity, conversation):
        _rule(
            tenancy.workspace,
            condition={"keywords": [{"text": "refund", "mode": "contains"}]},
            actions=[{"type": "add_label", "label_id": str(_label(tenancy.workspace).pk)}],
        )

        _deliver(connection, event(connection, text="hello there"))

        assert _labels_on(conversation) == []

    def test_a_disabled_rule_is_skipped(self, tenancy, connection, identity, conversation):
        _rule(
            tenancy.workspace,
            condition={"keywords": [{"text": "refund", "mode": "contains"}]},
            actions=[{"type": "add_label", "label_id": str(_label(tenancy.workspace).pk)}],
            enabled=False,
        )

        _deliver(connection, event(connection, text="i need a refund"))

        assert _labels_on(conversation) == []

    def test_every_matching_rule_applies(self, tenancy, connection, identity, conversation):
        """Rules are cumulative, unlike triggers. ``matching.match`` returns the
        first trigger that matches and stops; SPEC §14 evaluates all of these."""
        _rule(
            tenancy.workspace,
            condition={"keywords": [{"text": "refund", "mode": "contains"}]},
            actions=[{"type": "add_label", "label_id": str(_label(tenancy.workspace, "Refunds").pk)}],
            priority=0,
            name="First",
        )
        _rule(
            tenancy.workspace,
            condition={"channel": {"platforms": ["telegram"]}},
            actions=[{"type": "add_label", "label_id": str(_label(tenancy.workspace, "Telegram").pk)}],
            priority=10,
            name="Second",
        )

        _deliver(connection, event(connection, text="i need a refund"))

        assert _labels_on(conversation) == ["Refunds", "Telegram"]

    def test_rules_run_in_priority_order(self, tenancy, connection, identity, conversation):
        """Lower first, the same convention triggers use — and the ledger records
        the order they were claimed in."""
        _rule(
            tenancy.workspace,
            condition={"channel": {"platforms": ["telegram"]}},
            actions=[{"type": "add_label", "label_id": str(_label(tenancy.workspace, "Late").pk)}],
            priority=20,
            name="Late",
        )
        _rule(
            tenancy.workspace,
            condition={"channel": {"platforms": ["telegram"]}},
            actions=[{"type": "add_label", "label_id": str(_label(tenancy.workspace, "Early").pk)}],
            priority=1,
            name="Early",
        )

        _deliver(connection, event(connection, text="hi"))

        applied = list(
            InboxRuleApplication.objects.for_workspace(tenancy.workspace).order_by("created_at").select_related("rule")
        )
        assert [row.rule.name for row in applied] == ["Early", "Late"]

    def test_it_assigns_and_closes(self, tenancy, connection, identity, conversation):
        agent = tenancy.user_for("agent")
        _rule(
            tenancy.workspace,
            condition={"channel": {"platforms": ["telegram"]}},
            actions=[{"type": "assign_to_member", "user_id": str(agent.pk)}, {"type": "mark_done"}],
        )

        _deliver(connection, event(connection, text="hi"))

        conversation.refresh_from_db()
        assert conversation.assignee_id == agent.pk
        assert conversation.state == ConversationState.DONE


class TestWhatItRefusesToTouch:
    def test_it_ignores_an_event_with_no_conversation(self, tenancy, connection):
        """A comment has neither a contact nor a thread —
        ``apps.messaging.ingest`` deliberately creates neither — so there is
        nothing to label and nobody to assign it to."""
        _rule(
            tenancy.workspace,
            condition={"channel": {"platforms": ["telegram"]}},
            actions=[{"type": "add_label", "label_id": str(_label(tenancy.workspace).pk)}],
        )

        _deliver(connection, event(connection, text="hi", kind=EventType.COMMENT, comment_id="c-1"))

        assert not ConversationLabelLink.objects.for_workspace(tenancy.workspace).exists()

    def test_it_ignores_a_follow(self, tenancy, connection, identity, conversation):
        """SPEC §14 says "on inbound message". A follow is not one, and the same
        reasoning ``stages.DEFAULT_REPLY_EVENTS`` gives applies."""
        _rule(
            tenancy.workspace,
            condition={"channel": {"platforms": ["telegram"]}},
            actions=[{"type": "add_label", "label_id": str(_label(tenancy.workspace).pk)}],
        )

        _deliver(connection, event(connection, kind=EventType.FOLLOW))

        assert _labels_on(conversation) == []

    def test_it_never_takes_a_thread_off_the_person_holding_it(self, tenancy, connection, identity, conversation):
        """The failure mode with real teeth. This stage runs *during* an agent
        takeover (it is in RUNS_WHILE_PAUSED), so an unguarded assignment hands
        the thread back to the rule the moment the contact replies."""
        from apps.messaging.services import assign_conversation

        holder = tenancy.user_for("agent")
        assign_conversation(conversation, holder)
        _rule(
            tenancy.workspace,
            condition={"channel": {"platforms": ["telegram"]}},
            actions=[{"type": "assign_to_member", "user_id": str(tenancy.user_for("admin").pk)}],
        )

        _deliver(connection, event(connection, text="hi"))

        conversation.refresh_from_db()
        assert conversation.assignee_id == holder.pk

    def test_a_rule_in_another_workspace_never_runs(self, tenancy, other_tenancy, connection, identity, conversation):
        _rule(
            other_tenancy.workspace,
            condition={"channel": {"platforms": ["telegram"]}},
            actions=[{"type": "mark_done"}],
        )

        _deliver(connection, event(connection, text="hi"))

        conversation.refresh_from_db()
        assert conversation.state == ConversationState.OPEN


class TestItRunsWhilePaused:
    def test_labels_apply_during_an_agent_takeover(self, tenancy, connection, identity, conversation):
        """``RUNS_WHILE_PAUSED`` contains post_persist deliberately: "inbox rules
        are inbox features — labels, assignment — and suppressing those during a
        takeover would break the takeover." There is no pause check in the hook,
        and this is the test that says so."""
        from datetime import timedelta

        from apps.messaging.services import pause_automation

        pause_automation(conversation, timezone.now() + timedelta(hours=1))
        _rule(
            tenancy.workspace,
            condition={"channel": {"platforms": ["telegram"]}},
            actions=[{"type": "add_label", "label_id": str(_label(tenancy.workspace).pk)}],
        )

        _deliver(connection, event(connection, text="hi"))

        assert _labels_on(conversation) == ["Refunds"]


class TestItDoesNotDisturbTheChain:
    def test_the_hook_never_consumes_or_defers(self, tenancy, connection, identity, conversation):
        """Asserted on the return value directly, not inferred from behaviour: a
        ``Deferred`` here would hand the whole stage to the worker and arrange
        this hook's own re-entry, and a ``Consumed`` would silently swallow every
        keyword trigger in the workspace."""
        from apps.flows.triggers.budget import InlineBudget
        from apps.flows.triggers.context import RoutingMode, build_context

        _rule(
            tenancy.workspace,
            condition={"channel": {"platforms": ["telegram"]}},
            actions=[{"type": "mark_done"}],
        )
        context = build_context(connection, event(connection, text="hi"), InlineBudget.start(), mode=RoutingMode.INLINE)

        assert apply_inbox_rules(context) is None

    def test_a_rule_marking_done_does_not_block_trigger_matching(self, tenancy, connection, identity, conversation):
        """The issue asks for this to be documented; here it is pinned.

        The second assertion is the half that surprises people: ``send_outbound``
        calls ``open_conversation`` unconditionally and that **reopens** a done
        thread, so "mark done" means *done unless something answers*.
        """
        from apps.flows.models import Trigger, TriggerType
        from apps.flows.tests.support import graph, node, published_flow

        flow = published_flow(
            tenancy.workspace,
            graph([node("a", "send_message", {"blocks": [{"type": "text", "text": "hello"}]})]),
            name="Reply",
        )
        Trigger(
            flow=flow,
            type=TriggerType.KEYWORD,
            config_json={"keywords": [{"text": "refund", "mode": "contains"}]},
        ).save()
        _rule(
            tenancy.workspace,
            condition={"keywords": [{"text": "refund", "mode": "contains"}]},
            actions=[{"type": "mark_done"}],
        )

        with routing_adapter(connection.platform) as adapter:
            route_events(connection, [event(connection, text="i need a refund")])

        assert len(adapter.sends) == 1
        conversation.refresh_from_db()
        assert conversation.state == ConversationState.OPEN

    def test_one_broken_action_does_not_cost_the_others(self, tenancy, connection, identity, conversation, monkeypatch):
        """ "One broken inbox rule must not cost the reply" is the registry's
        backstop. This is the finer-grained promise: one broken *action* must not
        cost the other actions in the same rule."""
        from apps.inbox import routing

        monkeypatch.setattr(routing, "_add_labels", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
        _rule(
            tenancy.workspace,
            condition={"channel": {"platforms": ["telegram"]}},
            actions=[
                {"type": "add_label", "label_id": str(_label(tenancy.workspace).pk)},
                {"type": "mark_done"},
            ],
        )

        _deliver(connection, event(connection, text="hi"))

        conversation.refresh_from_db()
        assert _labels_on(conversation) == []
        assert conversation.state == ConversationState.DONE


class TestIdempotence:
    def test_the_inline_and_worker_paths_agree(self, tenancy, connection, identity, conversation):
        """Same event, two environments: outside a transaction with no lock, and
        inside one already holding the contact advisory lock."""
        _rule(
            tenancy.workspace,
            condition={"channel": {"platforms": ["telegram"]}},
            actions=[{"type": "add_label", "label_id": str(_label(tenancy.workspace).pk)}],
        )
        evt = event(connection, text="hi", event_id="evt-shared")

        _deliver(connection, evt)
        inline = _labels_on(conversation)

        ConversationLabelLink.objects.for_workspace(tenancy.workspace).all().delete()
        InboxRuleApplication.objects.for_workspace(tenancy.workspace).all().delete()
        _replay(tenancy.workspace, connection, evt)

        assert _labels_on(conversation) == inline == ["Refunds"]

    def test_a_deferral_at_this_stage_does_not_double_apply(self, tenancy, connection, identity, conversation):
        """The one real replay vector. ``run_stage`` stops at the first hook that
        defers, so every lower-priority hook has already run inline — and
        ``pipeline._route`` then hands the **whole stage** to the worker, which
        replays all of it. Without the ledger the label would be applied twice.
        """
        from apps.flows.triggers.hooks import Deferred

        _rule(
            tenancy.workspace,
            condition={"channel": {"platforms": ["telegram"]}},
            actions=[{"type": "add_label", "label_id": str(_label(tenancy.workspace).pk)}],
        )
        register_hook(lambda context: Deferred("probe"), stage=Stage.POST_PERSIST, name="probe_defer", priority=200)
        try:
            evt = event(connection, text="hi", event_id="evt-deferred")
            _deliver(connection, evt)
            queued = ScheduledAction.objects.for_workspace(tenancy.workspace).filter(type=ROUTE_EVENT).first()
            assert queued is not None
        finally:
            unregister_hook("probe_defer")

        with routing_adapter(connection.platform):
            handle_route_event(queued.payload, queued)

        assert (
            ConversationLabelLink.objects.for_workspace(tenancy.workspace).filter(conversation=conversation).count()
            == 1
        )

    def test_two_deliveries_of_the_same_event_claim_once(self, tenancy, connection, identity, conversation):
        _rule(
            tenancy.workspace,
            condition={"channel": {"platforms": ["telegram"]}},
            actions=[{"type": "add_label", "label_id": str(_label(tenancy.workspace).pk)}],
        )
        evt = event(connection, text="hi", event_id="evt-twice")

        _deliver(connection, evt)
        _deliver(connection, evt)

        assert InboxRuleApplication.objects.for_workspace(tenancy.workspace).count() == 1

    def test_a_different_event_claims_again(self, tenancy, connection, identity, conversation):
        """The ledger is keyed on the event, not on the rule: a rule that labelled
        yesterday's message must still label today's."""
        _rule(
            tenancy.workspace,
            condition={"channel": {"platforms": ["telegram"]}},
            actions=[{"type": "mark_done"}],
        )

        _deliver(connection, event(connection, text="hi", event_id="evt-1"))
        _deliver(connection, event(connection, text="hi", event_id="evt-2"))

        assert InboxRuleApplication.objects.for_workspace(tenancy.workspace).count() == 2
