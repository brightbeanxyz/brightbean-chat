"""Stage semantics end to end: SPEC §9.3's four steps, and §14's pause."""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.channels.events import EventType
from apps.common.platforms import Platform
from apps.flows.models import ExecutionStatus, FlowExecution, StartedBy, Trigger, TriggerType
from apps.flows.tests.routing_support import routing_adapter
from apps.flows.tests.support import graph, inbound, node, published_flow
from apps.flows.triggers.budget import InlineBudget
from apps.flows.triggers.context import RoutingMode, build_context
from apps.flows.triggers.hooks import Stage, register_hook
from apps.flows.triggers.pipeline import ROUTABLE_EVENTS, route_events

NOOP_ACTION = {"actions": [{"verb": "remove_tag", "tag": "not-a-tag-here"}]}
SEND = {"blocks": [{"type": "text", "text": "hello"}]}


def _send_flow(workspace, name="Reply"):
    """A one-node flow that sends a message. Synchronous-safe, so it runs inline."""
    return published_flow(workspace, graph([node("a", "send_message", SEND)]), name=name)


def _trigger(flow, trigger_type, config=None, *, priority=0, connection=None):
    trigger = Trigger(
        flow=flow,
        channel_connection=connection,
        type=trigger_type,
        config_json=config or {},
        priority=priority,
    )
    trigger.save()
    return trigger


def _identity(connection, contact, user="tg-1"):
    """The identity persistence would have written. Routing only ever reads it."""
    from apps.messaging.models import ContactChannelIdentity

    identity = ContactChannelIdentity(
        contact=contact,
        channel_connection=connection,
        platform_user_id=user,
        opt_in=True,
        opt_in_at=timezone.now(),
        opt_in_source="message_in",
        last_inbound_at=timezone.now(),
    )
    identity.save()
    return identity


def _conversation(connection, contact):
    """The conversation, creating one only if a send has not already opened it."""
    from apps.messaging.models import Conversation

    existing = (
        Conversation.objects.for_workspace(connection.workspace_id)
        .filter(contact=contact, channel_connection=connection)
        .first()
    )
    if existing is not None:
        return existing
    conversation = Conversation(contact=contact, channel_connection=connection)
    conversation.save()
    return conversation


def _route(connection, event):
    route_events(connection, [event])


@pytest.mark.django_db
class TestStageOrder:
    def test_the_five_stages_run_in_order(self, tenancy, connection, contact):
        seen: list[str] = []
        for stage in Stage:
            register_hook(
                lambda context, stage=stage: seen.append(str(stage)),
                stage=stage,
                name=f"probe-{stage}",
                priority=1,
            )
        _identity(connection, contact)
        _conversation(connection, contact)

        with routing_adapter(Platform.TELEGRAM):
            _route(connection, inbound(connection, text="anything"))

        assert seen == ["hard_optout", "post_persist", "resume", "trigger", "default_reply"]

    def test_a_delivery_receipt_routes_nowhere(self):
        assert EventType.DELIVERY_STATUS not in ROUTABLE_EVENTS

    def test_an_opt_out_event_never_reaches_trigger_matching(self, tenancy, connection, contact):
        """Persistence already recorded the opt-out. Without the hard_optout hook
        a keyword trigger on "STOP" would start a flow at somebody who just
        unsubscribed."""
        reached: list[str] = []
        register_hook(lambda context: reached.append("trigger"), stage=Stage.TRIGGER, name="probe", priority=1)
        _identity(connection, contact)

        with routing_adapter(Platform.TELEGRAM):
            _route(connection, inbound(connection, kind=EventType.OPT_OUT, text="STOP"))

        assert reached == []

    def test_one_bad_event_does_not_stop_the_rest_of_the_batch(self, tenancy, connection, contact):
        seen: list[str] = []

        def explodes(context):
            if context.event.provider_event_id == "bad":
                raise RuntimeError("boom")
            seen.append(context.event.provider_event_id)
            return None

        register_hook(explodes, stage=Stage.POST_PERSIST, name="probe", priority=1)
        _identity(connection, contact)

        with routing_adapter(Platform.TELEGRAM):
            route_events(
                connection,
                [
                    inbound(connection, text="a", event_id="bad"),
                    inbound(connection, text="b", event_id="good"),
                ],
            )

        assert seen == ["good"]


@pytest.mark.django_db
class TestTriggerStage:
    def test_a_keyword_trigger_fires_and_sends(self, tenancy, connection, contact):
        flow = _send_flow(tenancy.workspace)
        _trigger(flow, TriggerType.KEYWORD, {"keywords": [{"text": "help", "mode": "contains"}]})
        _identity(connection, contact)

        with routing_adapter(Platform.TELEGRAM) as adapter:
            _route(connection, inbound(connection, text="help please"))

        assert len(adapter.sends) == 1
        execution = FlowExecution.objects.for_workspace(tenancy.workspace).get()
        assert execution.started_by.startswith(StartedBy.TRIGGER)

    def test_a_matched_reply_is_consumed_and_the_keyword_does_not_fire(self, tenancy, connection, contact):
        """SPEC §9.3: a keyword works mid-flow only if nothing consumed the event."""
        waiting = published_flow(
            tenancy.workspace,
            graph(
                [node("a", "send_message", {**SEND, "buttons": [{"id": "yes", "label": "Yes", "action": "postback"}]})],
            ),
            name="Waiting",
        )
        keyword_flow = _send_flow(tenancy.workspace, name="Keyword")
        _trigger(keyword_flow, TriggerType.KEYWORD, {"keywords": [{"text": "yes", "mode": "contains"}]})
        _identity(connection, contact)

        from apps.flows.engine import start_flow

        with routing_adapter(Platform.TELEGRAM) as adapter:
            start_flow(contact, waiting, started_by=StartedBy.API, connection=connection)
            adapter.sends.clear()
            _route(connection, inbound(connection, button_id="yes"))

        assert FlowExecution.objects.for_workspace(tenancy.workspace).filter(flow=keyword_flow).count() == 0

    def test_an_unmatched_reply_falls_through_to_the_keyword(self, tenancy, connection, contact):
        """The other half of §9.3, and the subtler one."""
        waiting = published_flow(
            tenancy.workspace,
            graph(
                [node("a", "send_message", {**SEND, "buttons": [{"id": "yes", "label": "Yes", "action": "postback"}]})],
            ),
            name="Waiting",
        )
        keyword_flow = _send_flow(tenancy.workspace, name="Keyword")
        _trigger(keyword_flow, TriggerType.KEYWORD, {"keywords": [{"text": "help", "mode": "contains"}]})
        _identity(connection, contact)

        from apps.flows.engine import start_flow

        with routing_adapter(Platform.TELEGRAM):
            start_flow(contact, waiting, started_by=StartedBy.API, connection=connection)
            _route(connection, inbound(connection, text="help", event_id="evt-2"))

        assert FlowExecution.objects.for_workspace(tenancy.workspace).filter(flow=keyword_flow).exists()

    def test_a_referral_never_reaches_the_resume_stage(self, tenancy, connection, contact):
        """A referral offered to a buttons wait matches nothing, hits the retry
        path, and would be consumed by a retry prompt — swallowing the ref link
        the contact just clicked."""
        waiting = published_flow(
            tenancy.workspace,
            graph(
                [node("a", "send_message", {**SEND, "buttons": [{"id": "yes", "label": "Yes", "action": "postback"}]})],
            ),
            name="Waiting",
        )
        ref_flow = _send_flow(tenancy.workspace, name="Ref")
        _trigger(ref_flow, TriggerType.REF_URL, {"ref": "promo"})
        _identity(connection, contact)

        from apps.flows.engine import start_flow

        with routing_adapter(Platform.TELEGRAM):
            start_flow(contact, waiting, started_by=StartedBy.API, connection=connection)
            _route(connection, inbound(connection, kind=EventType.REFERRAL, ref="promo", event_id="evt-2"))

        assert FlowExecution.objects.for_workspace(tenancy.workspace).filter(flow=ref_flow).exists()


@pytest.mark.django_db
class TestDefaultReply:
    def _setup(self, tenancy, connection, contact):
        flow = _send_flow(tenancy.workspace, name="Fallback")
        _trigger(flow, TriggerType.DEFAULT_REPLY)
        _identity(connection, contact)
        return flow

    def test_it_answers_when_nothing_else_matched(self, tenancy, connection, contact):
        self._setup(tenancy, connection, contact)

        with routing_adapter(Platform.TELEGRAM) as adapter:
            _route(connection, inbound(connection, text="who are you"))

        assert len(adapter.sends) == 1

    def test_it_is_suppressed_within_24_hours(self, tenancy, connection, contact):
        self._setup(tenancy, connection, contact)

        with routing_adapter(Platform.TELEGRAM) as adapter:
            _route(connection, inbound(connection, text="one", event_id="e1"))
            _route(connection, inbound(connection, text="two", event_id="e2"))

        assert len(adapter.sends) == 1

    def test_it_answers_again_once_the_window_lapses(self, tenancy, connection, contact):
        """The clock is moved through the ORM — this project ships no freezer."""
        from apps.flows.models import DefaultReplyState

        self._setup(tenancy, connection, contact)

        with routing_adapter(Platform.TELEGRAM) as adapter:
            _route(connection, inbound(connection, text="one", event_id="e1"))
            DefaultReplyState.objects.for_workspace(tenancy.workspace).update(
                last_sent_at=timezone.now() - timedelta(hours=25)
            )
            _route(connection, inbound(connection, text="two", event_id="e2"))

        assert len(adapter.sends) == 2

    def test_the_guard_is_per_channel(self, tenancy, connection, contact):
        from apps.flows.tests.support import connection_for

        self._setup(tenancy, connection, contact)
        second = connection_for(tenancy.workspace, external_id="bot-second")
        _identity(second, contact, user="tg-1")

        with routing_adapter(Platform.TELEGRAM) as adapter:
            _route(connection, inbound(connection, text="one", event_id="e1"))
            _route(second, inbound(second, text="two", event_id="e2"))

        assert len(adapter.sends) == 2

    def test_a_flow_that_cannot_start_does_not_burn_the_guard(self, tenancy, connection, contact):
        """A claim taken and not spent is worse than no guard: it costs the
        contact their one reply for the day and sends them nothing. The claim and
        the start share a savepoint, so a start that fails releases it."""
        from apps.flows.models import DefaultReplyState, FlowStatus

        flow = self._setup(tenancy, connection, contact)
        # Archived after the trigger was made: the candidate query still finds a
        # published version, and start_flow then refuses it.
        type(flow).all_objects.filter(pk=flow.pk).update(status=FlowStatus.ARCHIVED)

        with routing_adapter(Platform.TELEGRAM) as adapter:
            _route(connection, inbound(connection, text="one", event_id="e1"))

        assert adapter.sends == []
        assert not DefaultReplyState.objects.for_workspace(tenancy.workspace).exists()

    def test_the_guard_survives_a_reply_that_did_go_out(self, tenancy, connection, contact):
        """The other half of the pair above — the savepoint must not roll back a
        claim whose flow started fine."""
        from apps.flows.models import DefaultReplyState

        self._setup(tenancy, connection, contact)

        with routing_adapter(Platform.TELEGRAM) as adapter:
            _route(connection, inbound(connection, text="one", event_id="e1"))

        assert len(adapter.sends) == 1
        assert DefaultReplyState.objects.for_workspace(tenancy.workspace).count() == 1

    def test_a_postback_is_not_answered_with_a_default_reply(self, tenancy, connection, contact):
        """A button press is never "I didn't understand you"."""
        self._setup(tenancy, connection, contact)

        with routing_adapter(Platform.TELEGRAM) as adapter:
            _route(connection, inbound(connection, button_id="anything"))

        assert adapter.sends == []


@pytest.mark.django_db
class TestCommentGuard:
    """SPEC §10's comment guards, claimed by the routing stage rather than by
    a platform matcher nobody has written yet."""

    def _instagram(self, tenancy):
        from apps.flows.tests.support import connection_for

        return connection_for(tenancy.workspace, platform=Platform.INSTAGRAM, external_id="ig-acme")

    def _comment_trigger(self, tenancy, *, once=True):
        from apps.flows.triggers.registry import spec_for

        flow = _send_flow(tenancy.workspace, name="Comment flow")
        config = spec_for(TriggerType.COMMENT).default_config()
        config["once_per_contact_per_post"] = once
        return _trigger(flow, TriggerType.COMMENT, config)

    def _comment(self, connection, *, comment_id="c-1", user="ig-1", post="p-1"):
        return inbound(
            connection,
            kind=EventType.COMMENT,
            text="what is the price?",
            event_id=f"evt-{comment_id or 'none'}",
            user=user,
            comment_id=comment_id,
            extra={"post_id": post, "parent_comment_id": ""},
        )

    def test_a_matched_comment_is_claimed(self, tenancy):
        from apps.flows.models import HandledComment

        instagram = self._instagram(tenancy)
        trigger = self._comment_trigger(tenancy)

        with routing_adapter(Platform.INSTAGRAM):
            _route(instagram, self._comment(instagram))

        row = HandledComment.objects.for_workspace(tenancy.workspace).get()
        assert row.comment_id == "c-1"
        assert row.post_id == "p-1"
        assert row.commenter_ref == "ig-1"
        assert row.trigger_id == trigger.pk

    def test_a_second_comment_from_the_same_person_is_not_claimed_again(self, tenancy):
        from apps.flows.models import HandledComment

        instagram = self._instagram(tenancy)
        self._comment_trigger(tenancy)

        with routing_adapter(Platform.INSTAGRAM):
            _route(instagram, self._comment(instagram, comment_id="c-1"))
            _route(instagram, self._comment(instagram, comment_id="c-2"))

        assert HandledComment.objects.for_workspace(tenancy.workspace).count() == 1

    def test_the_setting_lets_a_second_comment_through(self, tenancy):
        from apps.flows.models import HandledComment

        instagram = self._instagram(tenancy)
        self._comment_trigger(tenancy, once=False)

        with routing_adapter(Platform.INSTAGRAM):
            _route(instagram, self._comment(instagram, comment_id="c-1"))
            _route(instagram, self._comment(instagram, comment_id="c-2"))

        assert HandledComment.objects.for_workspace(tenancy.workspace).count() == 2

    def test_a_comment_past_the_deadline_is_not_claimed(self, tenancy):
        """Claiming would spend the once-per-post guard on a reply the platform
        will refuse."""
        from apps.flows.models import HandledComment
        from apps.flows.triggers.guards import PRIVATE_REPLY_WINDOW

        instagram = self._instagram(tenancy)
        self._comment_trigger(tenancy)
        event = self._comment(instagram)
        object.__setattr__(event, "timestamp", timezone.now() - PRIVATE_REPLY_WINDOW - timedelta(hours=1))

        with routing_adapter(Platform.INSTAGRAM):
            _route(instagram, event)

        assert not HandledComment.objects.for_workspace(tenancy.workspace).exists()

    def test_a_comment_from_a_known_contact_is_claimed_too(self, tenancy):
        """The gate used to be ``context.contact is None``, which conflated "is
        this a comment" with "is there anybody to run a flow for". SPEC §10's
        guards are properties of the comment, so a commenter who happened to
        have a contact row bypassed them entirely."""
        from apps.flows.models import HandledComment

        instagram = self._instagram(tenancy)
        self._comment_trigger(tenancy)
        contact = _contact_with_identity(tenancy, instagram, "ig-known")

        with routing_adapter(Platform.INSTAGRAM):
            _route(instagram, self._comment(instagram, user="ig-known"))

        row = HandledComment.objects.for_workspace(tenancy.workspace).get()
        assert row.commenter_ref == "ig-known"
        assert contact is not None

    def test_a_known_contacts_second_comment_on_the_post_is_refused(self, tenancy):
        """``once_per_contact_per_post`` — the setting that silently did
        nothing for this class of commenter."""
        from apps.flows.models import HandledComment

        instagram = self._instagram(tenancy)
        self._comment_trigger(tenancy)
        _contact_with_identity(tenancy, instagram, "ig-known")

        with routing_adapter(Platform.INSTAGRAM):
            _route(instagram, self._comment(instagram, comment_id="c-1", user="ig-known"))
            _route(instagram, self._comment(instagram, comment_id="c-2", user="ig-known"))

        assert HandledComment.objects.for_workspace(tenancy.workspace).count() == 1

    def test_a_comment_with_no_id_is_not_claimed(self, tenancy):
        """L5-A and L5-B fill payload.comment_id; until then there is nothing to
        key the guard on and nothing may be started."""
        from apps.flows.models import HandledComment

        instagram = self._instagram(tenancy)
        self._comment_trigger(tenancy)

        with routing_adapter(Platform.INSTAGRAM):
            _route(instagram, self._comment(instagram, comment_id=""))

        assert not HandledComment.objects.for_workspace(tenancy.workspace).exists()

    def test_a_deferred_comment_hook_reaches_the_worker(self, tenancy):
        """A hook on the contactless path is under the same contract as anywhere
        else: an L5 comment hook that cannot finish inline says ``Deferred`` and
        the event must reach the worker rather than ending here."""
        from apps.flows.triggers.handlers import ROUTE_EVENT
        from apps.flows.triggers.hooks import Deferred, Stage, register_hook
        from apps.queueing.models import ScheduledAction

        instagram = self._instagram(tenancy)
        register_hook(lambda context: Deferred("needs the api"), stage=Stage.TRIGGER, name="probe", priority=1)

        with routing_adapter(Platform.INSTAGRAM):
            _route(instagram, self._comment(instagram))

        queued = ScheduledAction.objects.for_workspace(tenancy.workspace).filter(type=ROUTE_EVENT)
        assert queued.count() == 1
        assert queued.get().payload["stage"] == "trigger"

    def test_claiming_creates_no_contact(self, tenancy):
        """apps/messaging/ingest.py's rule: one viral post must not become a
        contact-spam amplifier."""
        from apps.contacts.models import Contact

        instagram = self._instagram(tenancy)
        self._comment_trigger(tenancy)

        with routing_adapter(Platform.INSTAGRAM):
            _route(instagram, self._comment(instagram))

        assert Contact.objects.for_workspace(tenancy.workspace).count() == 0


@pytest.mark.django_db
class TestAutomationPause:
    def _paused(self, connection, contact, *, until):
        conversation = _conversation(connection, contact)
        # Written directly rather than through pause_automation() so the test
        # does not depend on the inbox's 30-minute constant (SPEC §14).
        type(conversation).all_objects.filter(pk=conversation.pk).update(automation_paused_until=until)
        return conversation

    def test_a_paused_conversation_suppresses_matching(self, tenancy, connection, contact):
        flow = _send_flow(tenancy.workspace)
        _trigger(flow, TriggerType.KEYWORD, {"keywords": [{"text": "help", "mode": "contains"}]})
        _identity(connection, contact)
        self._paused(connection, contact, until=timezone.now() + timedelta(minutes=30))

        with routing_adapter(Platform.TELEGRAM) as adapter:
            _route(connection, inbound(connection, text="help"))

        assert adapter.sends == []
        assert not FlowExecution.objects.for_workspace(tenancy.workspace).exists()

    def test_eligibility_returns_after_the_pause_lapses(self, tenancy, connection, contact):
        flow = _send_flow(tenancy.workspace)
        _trigger(flow, TriggerType.KEYWORD, {"keywords": [{"text": "help", "mode": "contains"}]})
        _identity(connection, contact)
        self._paused(connection, contact, until=timezone.now() - timedelta(minutes=1))

        with routing_adapter(Platform.TELEGRAM) as adapter:
            _route(connection, inbound(connection, text="help"))

        assert len(adapter.sends) == 1

    def test_a_waiting_execution_keeps_waiting_while_paused(self, tenancy, connection, contact):
        """ "Waiting executions do not consume events while paused" — SPEC §14.
        Nothing is written to record the skip, which is exactly why eligibility
        comes back on its own."""
        waiting = published_flow(
            tenancy.workspace,
            graph(
                [node("a", "send_message", {**SEND, "buttons": [{"id": "yes", "label": "Yes", "action": "postback"}]})],
            ),
            name="Waiting",
        )
        _identity(connection, contact)

        from apps.flows.engine import start_flow

        with routing_adapter(Platform.TELEGRAM):
            execution = start_flow(contact, waiting, started_by=StartedBy.API, connection=connection)
            self._paused(connection, contact, until=timezone.now() + timedelta(minutes=30))
            _route(connection, inbound(connection, button_id="yes", event_id="evt-2"))

        execution.refresh_from_db()
        assert execution.status == ExecutionStatus.WAITING_REPLY

    def test_opt_out_still_runs_while_paused(self, tenancy, connection, contact):
        seen: list[str] = []
        register_hook(lambda context: seen.append("optout"), stage=Stage.HARD_OPTOUT, name="probe", priority=1)
        register_hook(lambda context: seen.append("persist"), stage=Stage.POST_PERSIST, name="probe2", priority=1)
        register_hook(lambda context: seen.append("trigger"), stage=Stage.TRIGGER, name="probe3", priority=1)
        _identity(connection, contact)
        self._paused(connection, contact, until=timezone.now() + timedelta(minutes=30))

        with routing_adapter(Platform.TELEGRAM):
            _route(connection, inbound(connection, text="hello"))

        assert seen == ["optout", "persist"]


@pytest.mark.django_db
class TestContextBuilding:
    def test_a_comment_routes_without_a_contact_and_creates_none(self, tenancy):
        """Pins the contract in apps/messaging/ingest.py: a comment event
        persists nothing, so routing must not resolve one either — that is what
        keeps one viral post from becoming a contact-spam amplifier."""
        from apps.contacts.models import Contact
        from apps.flows.tests.support import connection_for

        instagram = connection_for(tenancy.workspace, platform=Platform.INSTAGRAM, external_id="ig-acme")
        context = build_context(
            instagram,
            inbound(instagram, kind=EventType.COMMENT, text="price?", user="ig-9"),
            InlineBudget.start(),
            mode=RoutingMode.INLINE,
        )

        assert context is not None
        assert context.contact is None
        assert context.can_run_engine is False
        assert Contact.objects.for_workspace(tenancy.workspace).count() == 0

    def test_an_event_naming_another_connection_is_dropped(self, tenancy, connection):
        from apps.flows.tests.support import connection_for

        other = connection_for(tenancy.workspace, external_id="bot-other")
        event = inbound(other, text="hi")

        assert build_context(connection, event, InlineBudget.start(), mode=RoutingMode.INLINE) is None

    def test_the_pause_is_fixed_when_the_context_is_built(self, tenancy, connection, contact):
        """Every stage gets the same answer even if the pause lapses mid-chain."""
        _identity(connection, contact)
        conversation = _conversation(connection, contact)
        type(conversation).all_objects.filter(pk=conversation.pk).update(
            automation_paused_until=timezone.now() + timedelta(minutes=5)
        )

        context = build_context(
            connection, inbound(connection, text="hi"), InlineBudget.start(), mode=RoutingMode.INLINE
        )
        assert context.is_paused is True


def _contact_with_identity(tenancy, connection, address):
    """A contact this connection already knows, so the comment path meets one.

    Through the facade (contract 1) rather than the ORM: the identity table has
    a check constraint tying ``opt_in`` to its audit fields, and a fixture that
    wrote the row directly would be a fixture that can drift from what the
    product produces.
    """
    from apps.contacts.services import create_contact
    from apps.messaging import services

    contact = create_contact(workspace=tenancy.workspace, first_name="Known")
    services.upsert_contact_identity(
        contact,
        connection.platform,
        address,
        source="message_in",
        opt_in=True,
        connection=connection,
    )
    return contact
