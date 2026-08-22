"""The deferred path: serialization, the worker handler, and exactly-once."""

import json

import pytest
from django.utils import timezone

from apps.channels.events import EventType
from apps.common.platforms import Platform
from apps.flows.models import FlowExecution, RoutedEvent, Trigger, TriggerType
from apps.flows.tests.routing_support import routing_adapter
from apps.flows.tests.support import connection_for, graph, inbound, node, published_flow
from apps.flows.triggers.handlers import ROUTE_EVENT, handle_route_event, route_idempotency_key
from apps.flows.triggers.serialization import (
    MAX_ROUTE_EXTRA_KEYS,
    MAX_ROUTE_PAYLOAD_BYTES,
    MAX_ROUTE_TEXT_CHARS,
    event_to_payload,
    payload_to_event,
    shrink_to_fit,
)
from apps.queueing.models import ScheduledAction
from apps.queueing.registry import registered_types

SEND = {"blocks": [{"type": "text", "text": "hello"}]}


def _identity(connection, contact, user="tg-1"):
    from apps.messaging.models import ContactChannelIdentity

    identity = ContactChannelIdentity(
        contact=contact,
        channel_connection=connection,
        platform_user_id=user,
        opt_in=True,
        opt_in_at=timezone.now(),
        opt_in_source="message_in",
    )
    identity.save()
    return identity


def _action(workspace, connection, event, stage="trigger"):
    return ScheduledAction.objects.create(
        workspace=workspace,
        type=ROUTE_EVENT,
        run_at=timezone.now(),
        payload={
            "stage": stage,
            "connection_id": str(connection.pk),
            "event": event_to_payload(event),
        },
    )


@pytest.mark.django_db
class TestSerialization:
    def test_every_field_routing_reads_round_trips(self, tenancy, connection):
        event = inbound(
            connection,
            text="help",
            button_id="yes",
            ref="promo",
            kind=EventType.POSTBACK,
            extra={"post_id": "p-1", "parent_comment_id": ""},
        )

        rebuilt = payload_to_event(event_to_payload(event), connection)

        assert rebuilt.type == EventType.POSTBACK
        assert rebuilt.payload.text == "help"
        assert rebuilt.payload.button_id == "yes"
        assert rebuilt.payload.ref == "promo"
        assert rebuilt.payload.extra["post_id"] == "p-1"
        assert rebuilt.provider_event_id == event.provider_event_id

    def test_raw_is_not_carried(self, tenancy, connection):
        """Unbounded, wholly attacker-controlled, read by no routing stage, and
        already stored verbatim in webhook_event_log (SECURITY-BASELINE §7)."""
        event = inbound(connection, text="hi")
        object.__setattr__(event, "raw", {"secret": "x" * 5000})

        payload = event_to_payload(event)

        assert "raw" not in payload
        assert "secret" not in str(payload)

    def test_text_is_capped(self, tenancy, connection):
        event = inbound(connection, text="x" * (MAX_ROUTE_TEXT_CHARS * 2))
        assert len(event_to_payload(event)["payload"]["text"]) == MAX_ROUTE_TEXT_CHARS

    def test_extra_keeps_only_bounded_scalars(self, tenancy, connection):
        event = inbound(
            connection,
            extra={
                "post_id": "p-1",
                "count": 3,
                "flag": True,
                "nested": {"no": "thanks"},
                "long": "y" * 5000,
                **{f"k{index}": index for index in range(50)},
            },
        )

        extra = event_to_payload(event)["payload"]["extra"]

        assert len(extra) <= MAX_ROUTE_EXTRA_KEYS
        assert "nested" not in extra
        assert all(isinstance(value, str | int | float | bool) for value in extra.values())

    @pytest.mark.parametrize(
        "payload",
        [
            None,
            "not a dict",
            {},
            {"type": "teleport"},
            {"type": "message", "payload": "not a dict"},
            {"type": "message", "platform_user_id": 42, "payload": {"text": ["nope"]}},
        ],
    )
    def test_a_malformed_payload_is_dropped_or_defaulted_never_raised(self, tenancy, connection, payload):
        """SECURITY-BASELINE §2: a document that has been sitting in a table is
        no more trustworthy than the request that created it."""
        rebuilt = payload_to_event(payload, connection)
        assert rebuilt is None or rebuilt.type == EventType.MESSAGE

    def test_an_oversized_payload_shrinks_in_a_documented_order(self, tenancy, connection):
        event = inbound(connection, text="x" * 2000, extra={"blob": "y" * 400})
        document = {
            "stage": "trigger",
            "connection_id": str(connection.pk),
            "event": event_to_payload(event),
        }
        document["event"]["payload"]["attachments"] = ["https://example.test/" + "z" * 1900] * 20

        shrunk = shrink_to_fit(document)

        assert shrunk is not None
        assert shrunk["event"]["payload"]["extra"] == {}
        assert shrunk["event"]["payload"]["attachments"] == []
        assert len(json.dumps(shrunk).encode()) <= MAX_ROUTE_PAYLOAD_BYTES

    def test_the_idempotency_key_hashes_the_event_id(self, tenancy, connection):
        """Attacker-controlled and unbounded, so it must not reach the column raw
        — and hashing rather than slicing keeps two long ids from colliding."""
        key = route_idempotency_key(connection, "e" * 5000, "resume")

        assert len(key) < 200
        assert "eeee" not in key
        assert key != route_idempotency_key(connection, "f" * 5000, "resume")
        assert key != route_idempotency_key(connection, "e" * 5000, "trigger")


@pytest.mark.django_db
class TestTheHandler:
    def test_it_is_registered(self):
        assert ROUTE_EVENT in registered_types()

    def test_it_finishes_the_routing_the_request_could_not(self, tenancy, connection, contact):
        flow = published_flow(tenancy.workspace, graph([node("a", "send_message", SEND)]), name="Reply")
        Trigger(
            flow=flow,
            type=TriggerType.KEYWORD,
            config_json={"keywords": [{"text": "help", "mode": "contains"}]},
        ).save()
        _identity(connection, contact)
        action = _action(tenancy.workspace, connection, inbound(connection, text="help"))

        with routing_adapter(Platform.TELEGRAM) as adapter:
            handle_route_event(action.payload, action)

        assert len(adapter.sends) == 1
        assert FlowExecution.objects.for_workspace(tenancy.workspace).count() == 1

    def test_running_it_twice_sends_once(self, tenancy, connection, contact):
        """SPEC §21: zero duplicate sends across forced worker retries.

        Zombie recovery resets a `running` row after ten minutes, so a slow
        handler can be claimed a second time. send_outbound's key is
        exec:{execution_id}:…, and a second start_flow mints a *new* execution
        id — so the RoutedEvent row is what makes this true, not the send key.
        """
        flow = published_flow(tenancy.workspace, graph([node("a", "send_message", SEND)]), name="Reply")
        Trigger(
            flow=flow,
            type=TriggerType.KEYWORD,
            config_json={"keywords": [{"text": "help", "mode": "contains"}]},
        ).save()
        _identity(connection, contact)
        action = _action(tenancy.workspace, connection, inbound(connection, text="help"))

        with routing_adapter(Platform.TELEGRAM) as adapter:
            handle_route_event(action.payload, action)
            handle_route_event(action.payload, action)

        assert len(adapter.sends) == 1
        assert RoutedEvent.objects.for_workspace(tenancy.workspace).count() == 1

    def test_it_resumes_from_the_named_stage(self, tenancy, connection, contact):
        """A hand-off at `trigger` must not re-run hard_optout or post_persist —
        L6-C's inbox rules would apply their labels a second time."""
        from apps.flows.triggers.hooks import Stage, register_hook

        seen: list[str] = []
        register_hook(lambda context: seen.append("persist"), stage=Stage.POST_PERSIST, name="probe", priority=1)
        _identity(connection, contact)
        action = _action(tenancy.workspace, connection, inbound(connection, text="hi"), stage="trigger")

        with routing_adapter(Platform.TELEGRAM):
            handle_route_event(action.payload, action)

        assert seen == []

    def test_a_connection_from_another_workspace_is_dropped(self, tenancy, other_tenancy, connection, contact):
        """The payload's ids are ids, never trusted objects — the rule
        apps/flows/handlers.py already applies."""
        theirs = connection_for(other_tenancy.workspace, external_id="bot-rival")
        action = _action(tenancy.workspace, connection, inbound(connection, text="hi"))
        action.payload["connection_id"] = str(theirs.pk)

        with routing_adapter(Platform.TELEGRAM) as adapter:
            handle_route_event(action.payload, action)

        assert adapter.sends == []
        assert not RoutedEvent.objects.for_workspace(tenancy.workspace).exists()

    @pytest.mark.parametrize("bad", [{}, {"stage": "nowhere"}, {"stage": "trigger"}])
    def test_a_payload_it_cannot_use_is_dropped_not_raised(self, tenancy, connection, bad):
        action = ScheduledAction.objects.create(
            workspace=tenancy.workspace, type=ROUTE_EVENT, run_at=timezone.now(), payload=bad
        )
        handle_route_event(bad, action)

    def test_the_worker_has_no_inline_budget(self, tenancy, connection, contact):
        """A smart_delay-first flow enqueues inline but must simply run here —
        there is no client waiting, so there is nothing to protect."""
        flow = published_flow(
            tenancy.workspace,
            graph([node("a", "smart_delay", {"mode": "duration", "duration": {"value": 1, "unit": "hours"}})]),
            name="Delayed",
        )
        Trigger(
            flow=flow,
            type=TriggerType.KEYWORD,
            config_json={"keywords": [{"text": "help", "mode": "contains"}]},
        ).save()
        _identity(connection, contact)
        action = _action(tenancy.workspace, connection, inbound(connection, text="help"))

        with routing_adapter(Platform.TELEGRAM):
            handle_route_event(action.payload, action)

        assert FlowExecution.objects.for_workspace(tenancy.workspace).count() == 1

    def test_it_does_not_re_enqueue_itself(self, tenancy, connection, contact):
        """hand_off is a no-op on the worker path — a handler re-enqueueing on a
        lock it already holds would be a loop."""
        _identity(connection, contact)
        action = _action(tenancy.workspace, connection, inbound(connection, text="nothing matches"))

        with routing_adapter(Platform.TELEGRAM):
            handle_route_event(action.payload, action)

        assert ScheduledAction.objects.for_workspace(tenancy.workspace).filter(type=ROUTE_EVENT).count() == 1
