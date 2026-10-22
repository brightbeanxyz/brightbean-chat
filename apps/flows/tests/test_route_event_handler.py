"""The deferred path: serialization, the worker handler, and exactly-once."""

import copy
import json

import pytest
from django.utils import timezone

from apps.channels.events import EventType
from apps.common.jsonsize import jsonb_byte_size
from apps.common.platforms import Platform
from apps.flows.models import FlowExecution, RoutedEvent, Trigger, TriggerType
from apps.flows.tests.routing_support import routing_adapter
from apps.flows.tests.support import connection_for, graph, inbound, node, published_flow
from apps.flows.triggers.handlers import ROUTE_EVENT, handle_route_event, route_idempotency_key
from apps.flows.triggers.serialization import (
    MAX_ROUTE_EXTRA_KEYS,
    MAX_ROUTE_PAYLOAD_BYTES,
    MAX_ROUTE_SHRUNK_TEXT_BYTES,
    MAX_ROUTE_TEXT_CHARS,
    _truncate_to_bytes,
    event_to_payload,
    payload_to_event,
    shrink_to_fit,
)
from apps.queueing.models import ScheduledAction
from apps.queueing.registry import registered_types

SEND = {"blocks": [{"type": "text", "text": "hello"}]}

EMOJI = "\U0001f600"
#: What `json.loads` returns for the escape `"\ud800"`: valid JSON, and a code
#: point `.encode("utf-8")` refuses. Any webhook body can carry one.
LONE_SURROGATE = json.loads('"\\ud800"')


def _document(connection, event):
    return {"stage": "trigger", "connection_id": str(connection.pk), "event": event_to_payload(event)}


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

    def test_an_overlong_platform_user_id_survives_as_the_stored_form(self, tenancy, connection):
        """Persistence stored this id through ``bounded_address``, which hashes
        rather than truncates. Slicing it here sent the worker looking for an
        identity row that does not exist, and the event routed with no contact."""
        from apps.messaging.identities import bounded_address

        raw = "u" * 500
        event = inbound(connection, text="hi", user=raw)

        stored = event_to_payload(event)["platform_user_id"]

        assert stored == bounded_address(raw)
        assert len(stored) <= 200

    def test_two_overlong_ids_sharing_a_prefix_stay_distinct(self, tenancy, connection):
        """Truncation would have collapsed them into one another, and one event
        would silently claim the other's RoutedEvent guard."""
        first = event_to_payload(inbound(connection, event_id="e" * 250 + "A", user="x" * 250 + "A"))
        second = event_to_payload(inbound(connection, event_id="e" * 250 + "B", user="x" * 250 + "B"))

        assert first["provider_event_id"] != second["provider_event_id"]
        assert first["platform_user_id"] != second["platform_user_id"]

    def test_bounding_is_idempotent_through_a_round_trip(self, tenancy, connection):
        event = inbound(connection, text="hi", user="u" * 500, event_id="e" * 500)

        once = event_to_payload(event)
        twice = event_to_payload(payload_to_event(once, connection))

        assert twice["platform_user_id"] == once["platform_user_id"]
        assert twice["provider_event_id"] == once["provider_event_id"]

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
        # Measured the way the column measures, not with `json.dumps`'s default
        # escaping: this assertion passed only because the fixture is ASCII,
        # and asserted a bound the code does not actually enforce.
        assert jsonb_byte_size(shrunk) <= MAX_ROUTE_PAYLOAD_BYTES

    def test_an_emoji_payload_is_not_shrunk_against_a_size_it_never_occupies(self, tenancy, connection):
        """The mirror of the test above: measuring an emoji as its twelve-byte
        escape rather than its four stored bytes sent a payload that already
        fit down the whole ladder — dropping `extra`, then the attachments,
        then truncating the very text the keyword matcher reads.

        The escaped-size assertion pins the fixture inside the window where the
        two measurements disagree; without it, a change to MAX_ROUTE_TEXT_CHARS
        or to the cap would leave a test that passes against the bug too.
        """
        text = EMOJI * 2000
        document = _document(connection, inbound(connection, text=text, extra={"blob": "y" * 100}))

        assert len(json.dumps(document, separators=(",", ":")).encode()) > MAX_ROUTE_PAYLOAD_BYTES

        shrunk = shrink_to_fit(document)

        assert shrunk is not None
        assert shrunk["event"]["payload"]["extra"] != {}
        assert shrunk["event"]["payload"]["text"] == text

    def test_a_lone_surrogate_is_refused_rather_than_raised(self, tenancy, connection):
        """`json.loads` hands one back from any webhook body, it survives
        `json.dumps`, and it raises UnicodeEncodeError from the encode. On this
        path that exception is not a finding — it is a 500 on an inbound
        request, taken while measuring a document nobody could have stored."""
        document = _document(connection, inbound(connection, text=LONE_SURROGATE))

        assert shrink_to_fit(document) is None

    def test_a_non_finite_number_is_refused_rather_than_queued(self, tenancy, connection):
        """`_extra` keeps raw floats and `json.dumps` writes bare NaN, so this
        used to measure small, queue cleanly, and be rejected by jsonb at the
        insert — after the webhook had already been answered."""
        document = _document(connection, inbound(connection, text="hi", extra={"score": float("nan")}))

        assert shrink_to_fit(document) is None

    def test_a_refused_document_is_not_left_stripped(self, tenancy, connection):
        """The rungs rewrite the payload in place and `body` aliased the
        caller's own dict, so a document this function ends up refusing came
        back already emptied of everything the rungs had tried."""
        event = inbound(connection, text="x" * 4000, extra={"blob": "y" * 400})
        document = _document(connection, event)
        document["event"]["payload"]["attachments"] = ["https://example.test/" + "z" * 1900] * 20
        before = copy.deepcopy(document)

        shrink_to_fit(document)

        assert document == before

    def test_the_text_rung_cuts_on_a_byte_budget_not_a_character_count(self, tenancy, connection):
        """MAX_ROUTE_TEXT_CHARS is in characters and the document cap is in
        bytes, so the widest legal text — 4096 emoji — is 16 KiB on its own and
        overruns the budget before an id is added. The rung is what reconciles
        the units, and the literal 1000 characters it replaces was safe only
        because 1000 emoji happens to be 4000 bytes."""
        document = _document(connection, inbound(connection, text=EMOJI * MAX_ROUTE_TEXT_CHARS))
        assert jsonb_byte_size(document) > MAX_ROUTE_PAYLOAD_BYTES

        shrunk = shrink_to_fit(document)

        assert shrunk is not None
        text = shrunk["event"]["payload"]["text"]
        assert len(text.encode("utf-8")) <= MAX_ROUTE_SHRUNK_TEXT_BYTES
        assert jsonb_byte_size(shrunk) <= MAX_ROUTE_PAYLOAD_BYTES
        # Byte-denominated, not the literal 1000 characters this replaced: the
        # budget buys 1024 four-byte emoji, and a character count would have
        # thrown away the last 24 of them for no reason anyone could state.
        assert len(text) == MAX_ROUTE_SHRUNK_TEXT_BYTES // 4 > 1000

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


class TestCuttingText:
    """The grapheme backstop on the truncation rung.

    Python strings are code points, so a slice cannot halve an emoji — but it
    can land inside a cluster that renders as one glyph, and each of these is
    reachable from an emoji keyboard.
    """

    @pytest.mark.parametrize(
        ("cluster", "label"),
        [
            ("\U0001f468\u200d\U0001f469\u200d\U0001f467", "ZWJ family"),
            ("\U0001f44d\U0001f3fd", "skin-tone modifier"),
            ("\U0001f1e9\U0001f1ea", "regional-indicator flag"),
            ("\u2764\ufe0f", "variation selector"),
            ("7\ufe0f\u20e3", "keycap"),
            ("e\u0301", "combining acute"),
        ],
    )
    def test_a_cluster_is_never_cut_in_half(self, cluster, label):
        """Every prefix length is tried, so the cut lands at every offset
        inside the cluster as well as between whole ones."""
        text = cluster * 6
        for limit in range(1, len(text.encode("utf-8")) + 1):
            cut = _truncate_to_bytes(text, limit)
            assert cut == cluster * (len(cut) // len(cluster)), f"{label} split at {limit} bytes"

    def test_it_respects_the_byte_budget(self):
        text = EMOJI * 50
        for limit in range(1, 200):
            assert len(_truncate_to_bytes(text, limit).encode("utf-8")) <= limit

    def test_plain_text_is_cut_where_the_budget_falls(self):
        assert _truncate_to_bytes("abcdef", 4) == "abcd"

    def test_text_inside_the_budget_is_returned_unchanged(self):
        assert _truncate_to_bytes(EMOJI * 3, 1000) == EMOJI * 3
