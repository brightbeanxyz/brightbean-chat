"""SPEC §10's ``rule`` trigger, bound to the internal event catalog.

The binding is a set of signal receivers, not a routing hook — ROADMAP contract 6
says so and a test below asserts it, because "we consumed the pipeline instead"
is the failure mode that would look fine until an event with no message arrived.

Everything here goes through the real emitters (``contacts.services.add_tag`` and
friends), so what is under test is the whole path: write → signal → candidate
query → filters → cooldown → ``start_flow``.
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.campaigns import services as campaign_services
from apps.campaigns.models import RuleTriggerFire
from apps.campaigns.rules import ACTION_RULE_EVENT, COOLDOWN, RULE_EVENT_FOR, claim_rule_fire, on_rule_event
from apps.campaigns.tests.support import contact_for, sequence_with
from apps.contacts import services as contact_services
from apps.contacts.models import CustomFieldType
from apps.flows.models import FlowExecution, FlowStatus, StartedBy, Trigger, TriggerType
from apps.flows.tests.support import graph, node, published_flow
from apps.queueing.models import ActionStatus, ActionType, ScheduledAction
from apps.queueing.worker import process_action


def _rule_flow(workspace, name="Rule flow", *, tag="fired"):
    return published_flow(
        workspace, graph([node("a", "action", {"actions": [{"verb": "add_tag", "tag": tag}]})]), name=name
    )


def _rule_trigger(workspace, config, *, flow=None, priority=0, enabled=True, name="Rule flow", tag="fired"):
    trigger = Trigger(
        flow=flow or _rule_flow(workspace, name=name, tag=tag),
        type=TriggerType.RULE,
        config_json=config,
        enabled=enabled,
        priority=priority,
    )
    trigger.save()
    return trigger


def drain(workspace):
    """Run the queued rule-trigger work, the way a worker would.

    The receiver on the catalog signal does not start a flow — it queues one
    ``rule_event`` row and returns, because it runs inside the transaction that
    wrote the tag and that write is very often one of thousands. So a test that
    wants to see the flow has to let the worker have its turn.

    ``start_flow`` rows are drained too: ``fire_trigger`` falls back to one when
    the contact lock is held, and a test should not depend on which path it took.
    """
    for _ in range(4):
        rows = list(
            ScheduledAction.objects.for_workspace(workspace)
            .filter(status=ActionStatus.PENDING, type__in=(ACTION_RULE_EVENT, ActionType.START_FLOW))
            .order_by("run_at")
        )
        if not rows:
            return
        for action in rows:
            action.status = ActionStatus.RUNNING
            action.save(update_fields=["status"])
            process_action(action)


def _executions(workspace):
    """Executions, after letting the worker drain what the event queued."""
    drain(workspace)
    return FlowExecution.objects.for_workspace(workspace)


def _tags(contact):
    """The contact's tags, after the worker has run whatever the event queued."""
    drain(contact.workspace_id)
    return {tag.name for tag in contact.tags.all()}


class TestTheBinding:
    def test_it_registers_no_routing_hook(self):
        """ROADMAP contract 6: rule triggers consume the event catalog, not the
        inbound pipeline. A hook registered from this app would fire on inbound
        messages and never on a tag added from the CRM — right-looking, wrong."""
        from apps.flows.triggers.hooks import registered_hooks

        owners = {registration.hook.__module__ for registration in registered_hooks()}

        assert not any(owner.startswith("apps.campaigns") for owner in owners)

    def test_it_covers_exactly_spec_ten_s_six_events(self):
        assert set(RULE_EVENT_FOR.values()) == {
            "contact_created",
            "tag_added",
            "tag_removed",
            "field_changed",
            "sequence_subscribed",
            "sequence_unsubscribed",
        }

    def test_the_schema_offers_the_same_six(self):
        """The trigger form and the matcher must not offer different lists."""
        from apps.flows.triggers.schema import RULE

        assert set(RULE["properties"]["event"]["enum"]) == set(RULE_EVENT_FOR.values())


@pytest.mark.django_db
class TestDeferral:
    """The receiver queues; the worker runs. See :func:`drain` above."""

    def test_the_receiver_queues_rather_than_starting_a_flow_inline(self, tenancy, django_assert_max_num_queries):
        """The emitter's hot path stays cheap.

        `contacts.imports` calls `create_contact` per CSV row and `bulk_tag`
        tags up to 500 contacts per request; a flow start in the receiver would
        put a whole graph execution inside each of those transactions. The
        receiver does what `apps/api/events.py::on_catalog_event` does with the
        same constraint: the cheap half here, the rest in the worker.
        """
        _rule_trigger(tenancy.workspace, {"event": "tag_added"})
        contact = contact_for(tenancy.workspace)
        tag, _ = contact_services.get_or_create_tag(tenancy.workspace, "VIP")

        contact_services.add_tag(contact, tag)

        assert not FlowExecution.objects.for_workspace(tenancy.workspace).exists()
        queued = ScheduledAction.objects.for_workspace(tenancy.workspace).filter(type=ACTION_RULE_EVENT).get()
        assert queued.contact_id == contact.pk
        assert queued.payload == {"event": "tag_added", "contact_id": str(contact.pk), "tag_id": str(tag.pk)}
        # Ids only, as contract 7 requires of a payload — including one sitting
        # in a queue table for a minute.
        assert "VIP" not in str(queued.payload)

    def test_a_workspace_with_no_rule_trigger_pays_one_query(self, tenancy, django_assert_num_queries):
        """The overwhelmingly common case: one indexed candidate lookup, no row."""
        contact = contact_for(tenancy.workspace)
        tag, _ = contact_services.get_or_create_tag(tenancy.workspace, "VIP")

        with django_assert_num_queries(1):
            on_rule_event(
                event="contact.tag_added",
                workspace_id=tenancy.workspace.pk,
                contact_id=contact.pk,
                tag_id=tag.pk,
            )

        assert not ScheduledAction.objects.for_workspace(tenancy.workspace).exists()

    def test_a_rolled_back_write_takes_the_queued_row_with_it(self, tenancy):
        """The property deferring must not lose: no run is ever announced for a
        change that did not happen."""
        from django.db import transaction

        _rule_trigger(tenancy.workspace, {"event": "tag_added"})
        contact = contact_for(tenancy.workspace)
        tag, _ = contact_services.get_or_create_tag(tenancy.workspace, "VIP")

        class RollbackError(Exception):
            pass

        with pytest.raises(RollbackError), transaction.atomic():
            contact_services.add_tag(contact, tag)
            raise RollbackError

        assert not ScheduledAction.objects.for_workspace(tenancy.workspace).filter(type=ACTION_RULE_EVENT).exists()


@pytest.mark.django_db
class TestTagEvents:
    def test_adding_a_tag_starts_the_rule_s_flow(self, tenancy):
        _rule_trigger(tenancy.workspace, {"event": "tag_added"})
        contact = contact_for(tenancy.workspace)
        tag, _ = contact_services.get_or_create_tag(tenancy.workspace, "VIP")

        contact_services.add_tag(contact, tag)

        assert _executions(tenancy.workspace).count() == 1
        assert "fired" in _tags(contact)

    def test_the_execution_is_stamped_with_the_trigger(self, tenancy):
        trigger = _rule_trigger(tenancy.workspace, {"event": "tag_added"})
        contact = contact_for(tenancy.workspace)
        tag, _ = contact_services.get_or_create_tag(tenancy.workspace, "VIP")

        contact_services.add_tag(contact, tag)

        assert _executions(tenancy.workspace).get().started_by == StartedBy.stamp(StartedBy.TRIGGER, trigger.pk)

    def test_re_adding_the_same_tag_does_not_fire_again(self, tenancy):
        """Exactly once per transition. The emitter is what guarantees it:
        ``add_tag`` sends nothing when the link is already there."""
        _rule_trigger(tenancy.workspace, {"event": "tag_added"})
        contact = contact_for(tenancy.workspace)
        tag, _ = contact_services.get_or_create_tag(tenancy.workspace, "VIP")

        contact_services.add_tag(contact, tag)
        contact_services.add_tag(contact, tag)

        assert _executions(tenancy.workspace).count() == 1

    def test_a_tag_id_filter_fires_only_for_that_tag(self, tenancy):
        """A contact-level condition cannot say this: it can only ask whether the
        contact has VIP *now*, which is true when any other tag is added too."""
        vip, _ = contact_services.get_or_create_tag(tenancy.workspace, "VIP")
        lead, _ = contact_services.get_or_create_tag(tenancy.workspace, "Lead")
        _rule_trigger(tenancy.workspace, {"event": "tag_added", "tag_id": str(vip.pk)})
        contact = contact_for(tenancy.workspace)

        contact_services.add_tag(contact, lead)
        assert not _executions(tenancy.workspace).exists()

        contact_services.add_tag(contact, vip)
        assert _executions(tenancy.workspace).count() == 1

    def test_an_id_filter_for_another_event_is_ignored(self, tenancy):
        """The schema permits `tag_id` on any rule, and the public API, a flow
        import or a client that changed the event without clearing the key can
        all produce one. Comparing it against a payload that never carries a
        tag id would make the rule silently unfireable."""
        vip, _ = contact_services.get_or_create_tag(tenancy.workspace, "VIP")
        plan = contact_services.create_custom_field(tenancy.workspace, name="Plan", field_type=CustomFieldType.TEXT)
        _rule_trigger(tenancy.workspace, {"event": "field_changed", "tag_id": str(vip.pk)})
        contact = contact_for(tenancy.workspace)

        contact_services.set_field_value(contact, plan, "gold")

        assert _executions(tenancy.workspace).count() == 1

    def test_tag_removed_is_its_own_event(self, tenancy):
        _rule_trigger(tenancy.workspace, {"event": "tag_removed"})
        contact = contact_for(tenancy.workspace)
        tag, _ = contact_services.get_or_create_tag(tenancy.workspace, "VIP")
        contact_services.add_tag(contact, tag)
        assert not _executions(tenancy.workspace).exists()

        contact_services.remove_tag(contact, tag)

        assert _executions(tenancy.workspace).count() == 1


@pytest.mark.django_db
class TestFieldAndContactEvents:
    def test_a_field_id_filter_fires_only_for_that_field(self, tenancy):
        plan = contact_services.create_custom_field(tenancy.workspace, name="Plan", field_type=CustomFieldType.TEXT)
        other = contact_services.create_custom_field(tenancy.workspace, name="Score", field_type=CustomFieldType.NUMBER)
        _rule_trigger(tenancy.workspace, {"event": "field_changed", "field_id": str(plan.pk)})
        contact = contact_for(tenancy.workspace)

        contact_services.set_field_value(contact, other, 5)
        assert not _executions(tenancy.workspace).exists()

        contact_services.set_field_value(contact, plan, "gold")
        assert _executions(tenancy.workspace).count() == 1

    def test_filters_match_the_new_value(self, tenancy):
        """SPEC §10's "field + new-value condition": the filter is evaluated
        after the change, so it reads what the field now holds."""
        plan = contact_services.create_custom_field(tenancy.workspace, name="Plan", field_type=CustomFieldType.TEXT)
        _rule_trigger(
            tenancy.workspace,
            {
                "event": "field_changed",
                "field_id": str(plan.pk),
                "filters": {
                    "match": "all",
                    "rules": [{"source": "custom_field", "key": str(plan.pk), "op": "is", "value": "gold"}],
                },
            },
        )
        contact = contact_for(tenancy.workspace)

        contact_services.set_field_value(contact, plan, "silver")
        assert not _executions(tenancy.workspace).exists()

        contact_services.set_field_value(contact, plan, "gold")
        assert _executions(tenancy.workspace).count() == 1

    def test_contact_created_fires_on_creation(self, tenancy):
        _rule_trigger(tenancy.workspace, {"event": "contact_created"})

        contact_services.create_contact(tenancy.workspace, first_name="New")

        assert _executions(tenancy.workspace).count() == 1

    def test_a_filter_that_cannot_be_evaluated_declines(self, tenancy, caplog):
        """Firing because the audience filter failed to compile is how a campaign
        reaches everybody."""
        from uuid import uuid4

        _rule_trigger(
            tenancy.workspace,
            {
                "event": "tag_added",
                "filters": {"match": "all", "rules": [{"source": "tag", "key": str(uuid4()), "op": "has"}]},
            },
        )
        contact = contact_for(tenancy.workspace)
        tag, _ = contact_services.get_or_create_tag(tenancy.workspace, "VIP")

        with caplog.at_level("WARNING"):
            contact_services.add_tag(contact, tag)

        assert not _executions(tenancy.workspace).exists()
        assert "cannot be evaluated" in caplog.text


@pytest.mark.django_db
class TestSequenceEvents:
    def test_subscribing_fires_a_sequence_subscribed_rule(self, tenancy):
        _rule_trigger(tenancy.workspace, {"event": "sequence_subscribed"})
        sequence = sequence_with(tenancy.workspace, steps=1, name="Onboarding")

        campaign_services.subscribe(sequence, contact_for(tenancy.workspace))

        assert _executions(tenancy.workspace).filter(flow__name="Rule flow").count() == 1

    def test_unsubscribing_fires_the_other_one(self, tenancy):
        _rule_trigger(tenancy.workspace, {"event": "sequence_unsubscribed"})
        sequence = sequence_with(tenancy.workspace, steps=2, name="Onboarding")
        contact = contact_for(tenancy.workspace)
        campaign_services.subscribe(sequence, contact)

        campaign_services.unsubscribe(sequence, contact)

        assert _executions(tenancy.workspace).filter(flow__name="Rule flow").count() == 1


@pytest.mark.django_db
class TestWhichTriggerWins:
    def test_the_lowest_priority_matching_trigger_wins_and_the_rest_do_not_run(self, tenancy):
        """SPEC §10: matching runs in priority order and the first match wins."""
        _rule_trigger(tenancy.workspace, {"event": "tag_added"}, priority=1, name="Second", tag="second")
        _rule_trigger(tenancy.workspace, {"event": "tag_added"}, priority=0, name="First", tag="first")
        contact = contact_for(tenancy.workspace)
        tag, _ = contact_services.get_or_create_tag(tenancy.workspace, "VIP")

        contact_services.add_tag(contact, tag)

        assert _tags(contact) == {"VIP", "first"}

    def test_a_non_matching_trigger_is_skipped_and_the_next_one_fires(self, tenancy):
        vip, _ = contact_services.get_or_create_tag(tenancy.workspace, "VIP")
        _rule_trigger(
            tenancy.workspace,
            {"event": "tag_added", "tag_id": str(vip.pk)},
            priority=0,
            name="Vip only",
            tag="vip-only",
        )
        _rule_trigger(tenancy.workspace, {"event": "tag_added"}, priority=1, name="Any tag", tag="any-tag")
        contact = contact_for(tenancy.workspace)
        lead, _ = contact_services.get_or_create_tag(tenancy.workspace, "Lead")

        contact_services.add_tag(contact, lead)

        assert _tags(contact) == {"Lead", "any-tag"}

    def test_a_disabled_trigger_does_not_fire(self, tenancy):
        _rule_trigger(tenancy.workspace, {"event": "tag_added"}, enabled=False)
        contact = contact_for(tenancy.workspace)
        tag, _ = contact_services.get_or_create_tag(tenancy.workspace, "VIP")

        contact_services.add_tag(contact, tag)

        assert not _executions(tenancy.workspace).exists()

    def test_a_trigger_on_a_flow_with_no_published_version_is_not_a_candidate(self, tenancy):
        """It would win the match and then swallow the event."""
        from apps.flows.services import create_flow

        draft = create_flow(workspace=tenancy.workspace, name="Draft")
        _rule_trigger(tenancy.workspace, {"event": "tag_added"}, flow=draft, priority=0)
        _rule_trigger(tenancy.workspace, {"event": "tag_added"}, priority=1, name="Live", tag="live")
        contact = contact_for(tenancy.workspace)
        tag, _ = contact_services.get_or_create_tag(tenancy.workspace, "VIP")

        contact_services.add_tag(contact, tag)

        assert "live" in _tags(contact)

    def test_an_archived_flow_is_not_a_candidate(self, tenancy):
        from apps.flows.models import Flow

        trigger = _rule_trigger(tenancy.workspace, {"event": "tag_added"})
        Flow.objects.for_workspace(tenancy.workspace).filter(pk=trigger.flow_id).update(status=FlowStatus.ARCHIVED)
        contact = contact_for(tenancy.workspace)
        tag, _ = contact_services.get_or_create_tag(tenancy.workspace, "VIP")

        contact_services.add_tag(contact, tag)

        assert not _executions(tenancy.workspace).exists()

    def test_another_workspace_s_trigger_never_sees_the_event(self, tenancy, other_tenancy):
        _rule_trigger(other_tenancy.workspace, {"event": "tag_added"})
        contact = contact_for(tenancy.workspace)
        tag, _ = contact_services.get_or_create_tag(tenancy.workspace, "VIP")

        contact_services.add_tag(contact, tag)

        assert not _executions(tenancy.workspace).exists()
        assert not _executions(other_tenancy.workspace).exists()


@pytest.mark.django_db
class TestTheCooldown:
    def test_a_second_fire_inside_the_window_is_suppressed(self, tenancy):
        """The loop breaker for the case transition semantics cannot catch: a
        flow that removes a tag and re-adds it produces a real transition every
        time."""
        _rule_trigger(tenancy.workspace, {"event": "tag_added"})
        contact = contact_for(tenancy.workspace)
        tag, _ = contact_services.get_or_create_tag(tenancy.workspace, "VIP")

        contact_services.add_tag(contact, tag)
        contact_services.remove_tag(contact, tag)
        contact_services.add_tag(contact, tag)

        assert _executions(tenancy.workspace).count() == 1

    def test_it_is_rolling_rather_than_clock_aligned(self, tenancy):
        trigger = _rule_trigger(tenancy.workspace, {"event": "tag_added"})
        contact = contact_for(tenancy.workspace)
        now = timezone.now()

        assert claim_rule_fire(trigger, contact, now=now) is True
        assert claim_rule_fire(trigger, contact, now=now + COOLDOWN / 2) is False
        assert claim_rule_fire(trigger, contact, now=now + COOLDOWN) is True

    def test_it_is_per_contact(self, tenancy):
        _rule_trigger(tenancy.workspace, {"event": "tag_added"})
        tag, _ = contact_services.get_or_create_tag(tenancy.workspace, "VIP")
        first = contact_for(tenancy.workspace, first_name="First")
        second = contact_for(tenancy.workspace, first_name="Second")

        contact_services.add_tag(first, tag)
        contact_services.add_tag(second, tag)

        assert _executions(tenancy.workspace).count() == 2

    def test_it_is_per_trigger(self, tenancy):
        trigger = _rule_trigger(tenancy.workspace, {"event": "tag_added"})
        other = _rule_trigger(tenancy.workspace, {"event": "tag_removed"}, name="Other")
        contact = contact_for(tenancy.workspace)
        now = timezone.now()

        assert claim_rule_fire(trigger, contact, now=now) is True
        assert claim_rule_fire(other, contact, now=now) is True

    def test_the_guard_row_belongs_to_the_contact_s_workspace(self, tenancy):
        trigger = _rule_trigger(tenancy.workspace, {"event": "tag_added"})
        contact = contact_for(tenancy.workspace)

        claim_rule_fire(trigger, contact)

        row = RuleTriggerFire.objects.for_workspace(tenancy.workspace).get()
        assert row.workspace_id == tenancy.workspace.pk
        assert row.trigger_id == trigger.pk

    def test_a_rule_whose_flow_re_adds_the_tag_terminates(self, tenancy):
        """The whole loop-safety story, end to end: a rule fired by ``tag_added``
        whose flow adds the same tag. The emitter's transition semantics stop the
        second pass; the cooldown is there for the case they cannot."""
        vip, _ = contact_services.get_or_create_tag(tenancy.workspace, "VIP")
        _rule_trigger(tenancy.workspace, {"event": "tag_added", "tag_id": str(vip.pk)}, tag="VIP")
        contact = contact_for(tenancy.workspace)

        contact_services.add_tag(contact, vip)

        assert _executions(tenancy.workspace).count() == 1
        assert _tags(contact) == {"VIP"}


@pytest.mark.django_db
class TestPruningTheCooldown:
    def test_it_drops_rows_past_the_window_and_keeps_live_ones(self, tenancy):
        """One row per (trigger, contact) that ever fired, updated in place — so
        without a prune the table grows to contacts x rule triggers and stays."""
        from apps.campaigns.housekeeping import PRUNE_MARGIN, prune_rule_trigger_fires

        trigger = _rule_trigger(tenancy.workspace, {"event": "tag_added"})
        stale = contact_for(tenancy.workspace, first_name="Stale")
        fresh = contact_for(tenancy.workspace, first_name="Fresh")
        claim_rule_fire(trigger, stale, now=timezone.now() - COOLDOWN - PRUNE_MARGIN - timedelta(minutes=1))
        claim_rule_fire(trigger, fresh)

        summary = prune_rule_trigger_fires()

        assert "1" in (summary or "")
        remaining = RuleTriggerFire.objects.for_workspace(tenancy.workspace)
        assert [row.contact_id for row in remaining] == [fresh.pk]

    def test_a_row_inside_the_window_is_never_pruned(self, tenancy):
        """Deleting at the boundary would let through a fire the guard refused."""
        from apps.campaigns.housekeeping import prune_rule_trigger_fires

        trigger = _rule_trigger(tenancy.workspace, {"event": "tag_added"})
        contact = contact_for(tenancy.workspace)
        claim_rule_fire(trigger, contact, now=timezone.now() - COOLDOWN)

        assert prune_rule_trigger_fires() is None
        assert RuleTriggerFire.objects.for_workspace(tenancy.workspace).count() == 1
