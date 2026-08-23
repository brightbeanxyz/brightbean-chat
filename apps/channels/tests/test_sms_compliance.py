"""SPEC §6.6's STOP/HELP/START, end to end through the real routing stages.

This is the issue's headline acceptance criterion — SPEC §21: "STOP suppresses
within one inbound event" — and it is tested through the production path rather
than by calling the hook: a signed Twilio form POST reaches the real endpoint,
which verifies it, dedups it, hands it to contract 6's seam, where L3-A's
persistence and L4-A's ordered stages run for real. The only substitution is the
network (``httpx.MockTransport``).

Testing it any other way would prove the wrong thing. The whole design claim is
that a carrier keyword is handled **before** trigger matching and that no flow
can intercept it, and that claim is about the ordering of five registered stages
— which only the real pipeline has.
"""

from collections.abc import Iterator
from typing import Any

import pytest

from apps.channels import ingest as channels_ingest
from apps.channels.models import (
    DEFAULT_HELP_TEXT,
    DEFAULT_OPT_IN_TEXT,
    DEFAULT_OPT_OUT_TEXT,
    ChannelConnection,
    SmsSettings,
)
from apps.channels.sms_compliance import HOOK_NAME, HOOK_PRIORITY, sms_keywords
from apps.channels.tests.sms_support import (
    CONTACT_NUMBER,
    FakeTwilio,
    Reply,
    fake_twilio,
    inbound_params,
    load_payload,
    signed_post,
    sms_connection,
)
from apps.common.platforms import Platform
from apps.flows.triggers.hooks import Stage, registered_hooks
from apps.messaging.ingest import PERSISTENCE_PROCESSOR, ROUTING_PROCESSOR, persist_events
from apps.messaging.models import (
    ContactChannelIdentity,
    Message,
    MessageDirection,
    MessageStatus,
    OptInSource,
)
from tests.support import Tenancy

pytestmark = pytest.mark.django_db


@pytest.fixture
def connection(tenancy: Tenancy) -> ChannelConnection:
    return sms_connection(tenancy.workspace)


@pytest.fixture(autouse=True)
def wired() -> Iterator[None]:
    """Persistence and the **real** routing tail, in contract 6's order.

    The app's own conftest clears the seam for every test in it (so the framework
    tests can assert on an empty one), which means an end-to-end test has to put
    back exactly what a running deployment has. Not a stand-in router: the
    property under test is the stage ordering itself.
    """
    from apps.flows.triggers.pipeline import route_events

    channels_ingest.register_processor(persist_events, name=PERSISTENCE_PROCESSOR)
    channels_ingest.register_processor(route_events, name=ROUTING_PROCESSOR)
    yield


@pytest.fixture
def twilio(monkeypatch: Any) -> Iterator[FakeTwilio]:
    with fake_twilio() as fake:
        yield fake


def deliver_pending(connection: ChannelConnection) -> None:
    """Refill the send bucket and run whatever the request handed to the worker.

    Not a workaround — it is the SMS platform behaving as specified, and a test
    that hid it would be testing a different channel. ``rate_default=1.0`` is
    Twilio's long-code throughput, so a connection's bucket holds about one
    token: the *first* reply in a second goes out inline, and SPEC §7.1's inline
    path then "falls back to enqueue when empty" for the next event. On a real
    deployment the worker picks it up a moment later; here, this does.

    Worth knowing which half is affected. Only the engine stages — resume,
    trigger, default_reply — are subject to that budget. ``hard_optout`` runs
    before the inline decision is even taken, which is why every compliance
    reply in this module lands without needing this.
    """
    from datetime import timedelta

    from django.utils import timezone

    from apps.flows.triggers.handlers import ROUTE_EVENT, handle_route_event
    from apps.messaging.models import SendBucket
    from apps.queueing.models import ActionStatus, ScheduledAction

    SendBucket.objects.filter(connection=connection).update(refilled_at=timezone.now() - timedelta(minutes=1))
    # The handler directly rather than ``worker.drain()``: the claim statement
    # is ``run_at <= now()``, and Postgres' ``now()`` is the *transaction* start
    # time — which inside pytest's per-test transaction predates every row this
    # test created, so a drain would find nothing due and quietly pass. Calling
    # the registered handler is what the routing suite already does.
    for action in ScheduledAction.objects.unscoped().filter(type=ROUTE_EVENT, status=ActionStatus.PENDING):
        handle_route_event(action.payload, action)


def identity_for(connection: ChannelConnection, number: str = CONTACT_NUMBER) -> ContactChannelIdentity:
    return ContactChannelIdentity.objects.for_workspace(connection.workspace_id).get(
        channel_connection=connection, platform_user_id=number
    )


def outbound_bodies(connection: ChannelConnection) -> list[str]:
    """What actually went to Twilio, in order."""
    return [
        block["text"]
        for message in Message.objects.for_workspace(connection.workspace_id)
        .filter(direction=MessageDirection.OUT)
        .order_by("created_at")
        for block in message.body.get("blocks", [])
        if block.get("type") == "text"
    ]


class TestRegistration:
    def test_the_hook_sits_at_hard_optout_before_the_builtin(self) -> None:
        """Contract 6's promise, asserted rather than assumed: L5-D registers
        rather than editing routing code, and it runs before
        ``stages.opt_out_event`` so it can answer the event that hook consumes.
        """
        names = [item.name for item in registered_hooks(Stage.HARD_OPTOUT)]

        assert HOOK_NAME in names
        assert names.index(HOOK_NAME) < names.index("opt_out_event")
        assert HOOK_PRIORITY < 100

    def test_it_is_the_only_stage_it_is_registered_at(self) -> None:
        from apps.flows.triggers.hooks import registered_hooks as all_hooks

        assert [item.stage for item in all_hooks() if item.name == HOOK_NAME] == [Stage.HARD_OPTOUT]

    def test_it_passes_on_every_other_platform(self, tenancy: Tenancy) -> None:
        """It runs for every inbound event in the deployment, so the first check
        has to be the cheapest one."""
        from apps.flows.triggers.hooks import Passed

        class _Context:
            connection = type("C", (), {"platform": Platform.TELEGRAM.value})()

        assert isinstance(sms_keywords(_Context()), Passed)


class TestStop:
    def test_stop_suppresses_and_confirms_within_one_inbound_event(
        self, client: Any, connection: ChannelConnection, twilio: FakeTwilio
    ) -> None:
        """SPEC §21's acceptance criterion, in one assertion block."""
        response = signed_post(client, connection, load_payload("inbound_stop"))

        assert response.status_code == 200
        identity = identity_for(connection)
        assert identity.opted_out_at is not None
        assert identity.opt_in is False
        assert outbound_bodies(connection) == [DEFAULT_OPT_OUT_TEXT]
        assert twilio.forms("Messages.json")[0]["Body"] == [DEFAULT_OPT_OUT_TEXT]

    def test_the_confirmation_goes_out_despite_the_suppression(
        self, client: Any, connection: ChannelConnection, twilio: FakeTwilio
    ) -> None:
        """The one message that has to survive the block it announces."""
        signed_post(client, connection, load_payload("inbound_stop"))

        confirmation = Message.objects.for_workspace(connection.workspace_id).get(direction=MessageDirection.OUT)
        assert confirmation.status == MessageStatus.SENT
        assert confirmation.error == ""

    def test_the_contacts_stop_lands_in_the_thread(
        self, client: Any, connection: ChannelConnection, twilio: FakeTwilio
    ) -> None:
        """``ingest`` writes no message row for an opt-out and returns early, so
        without the adapter also emitting the message half an agent opening the
        conversation saw our confirmation with nothing above it explaining why.
        """
        signed_post(client, connection, load_payload("inbound_stop"))

        inbound = Message.objects.for_workspace(connection.workspace_id).filter(direction=MessageDirection.IN)
        assert inbound.count() == 1
        assert inbound.get().body["blocks"][0]["text"].strip() == "Stop."

    def test_the_stop_message_still_never_starts_a_flow(
        self, client: Any, connection: ChannelConnection, twilio: FakeTwilio, tenancy: Tenancy
    ) -> None:
        """The message half reaches the routing stages like any other, so the
        hook has to consume it — otherwise putting the STOP in the thread would
        have re-opened the exact hole ``stages.opt_out_event`` closes."""
        flow = _published_flow(tenancy, "Stop flow", "You said stop")
        _keyword_trigger(flow, "stop")
        _keyword_trigger(_published_flow(tenancy, "Catch-all", "Hello there"), "sto")

        signed_post(client, connection, load_payload("inbound_stop"))

        from apps.flows.models import FlowExecution

        assert not FlowExecution.objects.for_workspace(tenancy.workspace).exists()
        assert outbound_bodies(connection) == [DEFAULT_OPT_OUT_TEXT]

    def test_a_later_send_is_blocked(self, client: Any, connection: ChannelConnection, twilio: FakeTwilio) -> None:
        from apps.channels.events import OutboundMessage, TextBlock
        from apps.messaging.services import send_outbound

        signed_post(client, connection, load_payload("inbound_stop"))
        identity = identity_for(connection)

        blocked = send_outbound(
            workspace=connection.workspace,
            contact=identity.contact,
            connection=connection,
            outbound=OutboundMessage(blocks=(TextBlock(text="Buy things"),)),
            source="automation",
            idempotency_key="after-stop",
        )

        assert blocked.status == MessageStatus.FAILED
        assert blocked.error == "opted_out"

    def test_a_broadcast_is_blocked_too(self, client: Any, connection: ChannelConnection, twilio: FakeTwilio) -> None:
        """Compliance is one chokepoint, so it cannot be true for one source only."""
        from apps.channels.events import OutboundMessage, TextBlock
        from apps.messaging.services import send_outbound

        signed_post(client, connection, load_payload("inbound_stop"))
        identity = identity_for(connection)

        blocked = send_outbound(
            workspace=connection.workspace,
            contact=identity.contact,
            connection=connection,
            outbound=OutboundMessage(blocks=(TextBlock(text="Sale!"),)),
            source="broadcast",
            idempotency_key="broadcast-after-stop",
        )

        assert blocked.error == "opted_out"

    def test_a_stop_never_reaches_the_trigger_stage(
        self, client: Any, connection: ChannelConnection, twilio: FakeTwilio, tenancy: Tenancy
    ) -> None:
        """A keyword trigger on the word STOP must not cheerfully start a flow
        at somebody who just unsubscribed (``stages.opt_out_event``)."""
        flow = _published_flow(tenancy, "Stop flow", "You said stop")
        _keyword_trigger(flow, "stop")

        signed_post(client, connection, load_payload("inbound_stop"))

        from apps.flows.models import FlowExecution

        assert not FlowExecution.objects.for_workspace(tenancy.workspace).exists()

    def test_a_redelivered_stop_confirms_once(
        self, client: Any, connection: ChannelConnection, twilio: FakeTwilio
    ) -> None:
        params = load_payload("inbound_stop")
        signed_post(client, connection, params)
        signed_post(client, connection, params)

        assert len(outbound_bodies(connection)) == 1

    def test_a_second_genuine_stop_is_confirmed_again(
        self, client: Any, connection: ChannelConnection, twilio: FakeTwilio
    ) -> None:
        """Carriers expect every STOP to be answered; only a redelivery is not."""
        signed_post(client, connection, load_payload("inbound_stop"))
        signed_post(client, connection, inbound_params(body="STOP", sid="SMsecondstop"))

        assert len(outbound_bodies(connection)) == 2

    def test_the_opt_out_timestamp_is_not_moved_by_a_second_stop(
        self, client: Any, connection: ChannelConnection, twilio: FakeTwilio
    ) -> None:
        """The first refusal is the one the audit is asking about."""
        signed_post(client, connection, load_payload("inbound_stop"))
        first = identity_for(connection).opted_out_at

        signed_post(client, connection, inbound_params(body="STOP", sid="SMsecondstop"))

        assert identity_for(connection).opted_out_at == first

    def test_workspace_wording_is_used_when_set(
        self, client: Any, connection: ChannelConnection, twilio: FakeTwilio
    ) -> None:
        SmsSettings.objects.create(workspace=connection.workspace, opt_out_confirmation="Bye from Acme.")

        signed_post(client, connection, load_payload("inbound_stop"))

        assert outbound_bodies(connection) == ["Bye from Acme."]


class TestHelp:
    def test_help_is_answered_and_consumed(
        self, client: Any, connection: ChannelConnection, twilio: FakeTwilio
    ) -> None:
        signed_post(client, connection, load_payload("inbound_help"))

        assert outbound_bodies(connection) == [DEFAULT_HELP_TEXT]

    def test_help_is_answered_after_an_opt_out(
        self, client: Any, connection: ChannelConnection, twilio: FakeTwilio
    ) -> None:
        """Carriers require HELP to work whatever else has happened, which is the
        whole reason ``services.send_compliance_reply`` exists."""
        signed_post(client, connection, load_payload("inbound_stop"))
        signed_post(client, connection, inbound_params(body="HELP", sid="SMhelp"))

        assert outbound_bodies(connection) == [DEFAULT_OPT_OUT_TEXT, DEFAULT_HELP_TEXT]

    def test_help_never_reaches_a_keyword_trigger(
        self, client: Any, connection: ChannelConnection, twilio: FakeTwilio, tenancy: Tenancy
    ) -> None:
        flow = _published_flow(tenancy, "Help flow", "Here is our FAQ")
        _keyword_trigger(flow, "help")

        signed_post(client, connection, load_payload("inbound_help"))

        from apps.flows.models import FlowExecution

        assert not FlowExecution.objects.for_workspace(tenancy.workspace).exists()
        assert outbound_bodies(connection) == [DEFAULT_HELP_TEXT]

    def test_help_still_lands_in_the_thread_as_an_inbound_message(
        self, client: Any, connection: ChannelConnection, twilio: FakeTwilio
    ) -> None:
        """An agent reading the conversation should see the question and the answer."""
        signed_post(client, connection, load_payload("inbound_help"))

        inbound = Message.objects.for_workspace(connection.workspace_id).filter(direction=MessageDirection.IN)
        assert inbound.count() == 1


class TestStart:
    def test_start_restores_consent_with_an_audit(
        self, client: Any, connection: ChannelConnection, twilio: FakeTwilio
    ) -> None:
        signed_post(client, connection, load_payload("inbound_stop"))
        before = identity_for(connection)
        assert before.opted_out_at is not None

        signed_post(client, connection, inbound_params(body="START", sid="SMstart"))

        identity = identity_for(connection)
        assert identity.opted_out_at is None
        assert identity.opt_in is True
        assert identity.opt_in_at is not None
        assert identity.opt_in_source == OptInSource.MESSAGE_IN

    def test_start_confirms(self, client: Any, connection: ChannelConnection, twilio: FakeTwilio) -> None:
        signed_post(client, connection, load_payload("inbound_stop"))
        signed_post(client, connection, inbound_params(body="unstop", sid="SMstart"))

        assert outbound_bodies(connection) == [DEFAULT_OPT_OUT_TEXT, DEFAULT_OPT_IN_TEXT]

    def test_sends_work_again_afterwards(self, client: Any, connection: ChannelConnection, twilio: FakeTwilio) -> None:
        """The compliance verdict is the claim, not the delivery.

        Asserted through ``can_send`` **and** through a real send, because the
        two answer different questions. The send is checked for "not refused"
        rather than for ``sent``: SMS runs a one-per-second token bucket
        (``rate_default=1.0``) and two confirmations have already gone out in
        this test, so a third message inside the same second is legitimately
        ``queued`` for the worker — which is the bucket working, not a block.
        """
        from apps.channels.events import OutboundMessage, TextBlock
        from apps.messaging.compliance import Allowed, can_send
        from apps.messaging.services import send_outbound

        signed_post(client, connection, load_payload("inbound_stop"))
        signed_post(client, connection, inbound_params(body="START", sid="SMstart"))
        identity = identity_for(connection)
        outbound = OutboundMessage(blocks=(TextBlock(text="Welcome back"),))

        assert isinstance(can_send(identity, "automation", outbound), Allowed)

        allowed = send_outbound(
            workspace=connection.workspace,
            contact=identity.contact,
            connection=connection,
            outbound=outbound,
            source="automation",
            idempotency_key="after-start",
        )

        assert allowed.status != MessageStatus.FAILED
        assert allowed.error != "opted_out"

    def test_an_ordinary_message_after_a_stop_does_not_re_consent(
        self, client: Any, connection: ChannelConnection, twilio: FakeTwilio
    ) -> None:
        """``record_consent`` only ever adds consent, and treating any message as
        re-consent is the bug that makes an opt-out look optional."""
        signed_post(client, connection, load_payload("inbound_stop"))
        signed_post(client, connection, inbound_params(body="are you there?", sid="SMchat"))

        identity = identity_for(connection)
        assert identity.opted_out_at is not None
        assert identity.opt_in is False


class TestFailureIsolation:
    def test_a_failed_confirmation_does_not_abandon_the_event(
        self, client: Any, connection: ChannelConnection, monkeypatch: Any
    ) -> None:
        """A raising ``hard_optout`` hook aborts the event by design (fail
        closed). That is right for the *check* and wrong for the *reply*:
        suppression already happened in persistence, so a Twilio outage must not
        stop inbound SMS being routed deployment-wide.
        """
        fake = FakeTwilio()
        fake.reply("Messages.json", Reply({"code": 20500}, status=500))

        with fake_twilio(fake):
            response = signed_post(client, connection, load_payload("inbound_stop"))

        assert response.status_code == 200
        assert identity_for(connection).opted_out_at is not None

    def test_an_unreadable_settings_row_falls_back_to_the_defaults(
        self, client: Any, connection: ChannelConnection, twilio: FakeTwilio, monkeypatch: Any
    ) -> None:
        """A database problem reading optional copy must not cost the reply that
        copy is for. Patched at the name the hook reads, so nothing else in the
        request is affected."""

        class _Exploding:
            @staticmethod
            def for_workspace(*args: Any, **kwargs: Any) -> Any:
                raise RuntimeError("the settings table is unreachable")

        # The manager only. The model class itself has to stay usable, because
        # the fallback the hook reaches for is an unsaved instance of it.
        monkeypatch.setattr(SmsSettings, "objects", _Exploding)

        signed_post(client, connection, load_payload("inbound_stop"))

        assert outbound_bodies(connection) == [DEFAULT_OPT_OUT_TEXT]


class TestOrdinaryTraffic:
    def test_a_keyword_trigger_fires_on_an_inbound_sms_exactly_like_a_dm(
        self, client: Any, connection: ChannelConnection, twilio: FakeTwilio, tenancy: Tenancy
    ) -> None:
        """SPEC §6.6: "run through the trigger matcher exactly like a DM"."""
        flow = _published_flow(tenancy, "Pricing", "Plans start at 10.")
        _keyword_trigger(flow, "pricing")

        signed_post(client, connection, inbound_params(body="pricing please", sid="SMkw"))

        assert outbound_bodies(connection) == ["Plans start at 10."]

    def test_an_ordinary_message_is_not_touched_by_the_hook(
        self, client: Any, connection: ChannelConnection, twilio: FakeTwilio
    ) -> None:
        signed_post(client, connection, load_payload("inbound_text"))

        assert outbound_bodies(connection) == []
        assert identity_for(connection).opted_out_at is None


class TestNumberedOptions:
    """SPEC §6.1's downgrade, closing the loop back through the resume stage.

    SMS declares ``buttons=False``, so the shared renderer appends "Reply 1
    for …" to the text and hands back the mapping; ``send_message`` writes that
    mapping into the wait config, and ``attempt_resume`` matches a digit against
    it. Nothing in the SMS adapter implements any of that — which is the point,
    and is why this test lives beside the adapter rather than in the engine's
    suite: it is the proof that a platform with no buttons still works.
    """

    def test_a_digit_reply_resumes_the_waiting_button_node(
        self, client: Any, connection: ChannelConnection, twilio: FakeTwilio, tenancy: Tenancy
    ) -> None:
        from apps.flows.models import ExecutionStatus, FlowExecution

        flow = _button_flow(tenancy)
        _keyword_trigger(flow, "menu")

        signed_post(client, connection, inbound_params(body="menu", sid="SMmenu"))

        # What Twilio received, not what the message row holds: the row stores
        # the *abstract* message with its buttons intact, and the numbering is
        # the adapter's rendering of it. That distinction is the whole design —
        # one message rendered once, downgraded per platform.
        asked = twilio.forms("Messages.json")[0]["Body"][0]
        assert "Reply 1 for Yes" in asked
        assert "Reply 2 for No" in asked
        execution = FlowExecution.objects.for_workspace(tenancy.workspace).get()
        assert execution.status == ExecutionStatus.WAITING_REPLY

        signed_post(client, connection, inbound_params(body="2", sid="SMtwo"))
        deliver_pending(connection)

        assert "Sorry to hear it." in outbound_bodies(connection)

    def test_an_unmatched_reply_still_falls_through_to_a_keyword_trigger(
        self, client: Any, connection: ChannelConnection, twilio: FakeTwilio, tenancy: Tenancy
    ) -> None:
        """SPEC §9.3: "keywords still work mid-flow only if nothing consumed the
        event". A digit nobody offered is not an answer."""
        flow = _button_flow(tenancy)
        _keyword_trigger(flow, "menu")
        _keyword_trigger(_published_flow(tenancy, "Pricing", "Plans start at 10."), "pricing")

        signed_post(client, connection, inbound_params(body="menu", sid="SMmenu"))
        signed_post(client, connection, inbound_params(body="pricing", sid="SMprice"))
        deliver_pending(connection)

        assert "Plans start at 10." in outbound_bodies(connection)


def _button_flow(tenancy: Tenancy) -> Any:
    from apps.flows.services import create_flow, publish, save_draft
    from apps.flows.tests.support import edge, graph, node

    flow = create_flow(workspace=tenancy.workspace, name="Menu")
    save_draft(
        flow,
        graph(
            [
                node(
                    "ask",
                    "send_message",
                    {
                        "blocks": [{"type": "text", "text": "Are you happy?"}],
                        "buttons": [
                            {"id": "yes", "label": "Yes", "action": "postback"},
                            {"id": "no", "label": "No", "action": "postback"},
                        ],
                    },
                ),
                node("said_yes", "send_message", {"blocks": [{"type": "text", "text": "Glad to hear it."}]}, x=200),
                node("said_no", "send_message", {"blocks": [{"type": "text", "text": "Sorry to hear it."}]}, x=400),
            ],
            [edge("ask", "btn:yes", "said_yes"), edge("ask", "btn:no", "said_no")],
        ),
        user=tenancy.owner,
    )
    publish(flow, user=tenancy.owner)
    return flow


def _published_flow(tenancy: Tenancy, name: str, reply: str) -> Any:
    from apps.flows.services import create_flow, publish, save_draft
    from apps.flows.tests.support import graph, node

    flow = create_flow(workspace=tenancy.workspace, name=name)
    save_draft(
        flow,
        graph([node("a", "send_message", {"blocks": [{"type": "text", "text": reply}]})]),
        user=tenancy.owner,
    )
    publish(flow, user=tenancy.owner)
    return flow


def _keyword_trigger(flow: Any, word: str) -> Any:
    from apps.flows.models import Trigger, TriggerType

    trigger = Trigger(
        flow=flow,
        type=TriggerType.KEYWORD,
        config_json={"keywords": [{"text": word, "mode": "contains"}]},
    )
    trigger.save()
    return trigger
