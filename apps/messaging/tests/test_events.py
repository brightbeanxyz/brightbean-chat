"""``message.received`` (ROADMAP contract 7)."""

from typing import Any
from uuid import uuid4

import pytest

from apps.channels.events import EventType
from apps.messaging.events import EVENT_MESSAGE_RECEIVED, MESSAGING_EVENT_NAMES, emit, message_received
from apps.messaging.ingest import persist_events
from apps.messaging.models import Conversation, Message
from apps.messaging.tests.conftest import make_event

pytestmark = pytest.mark.django_db


@pytest.fixture
def received() -> Any:
    """Collect every message.received payload for the duration of a test."""
    seen: list[dict[str, Any]] = []

    def receiver(sender: Any, **kwargs: Any) -> None:
        seen.append(kwargs)

    message_received.connect(receiver)
    try:
        yield seen
    finally:
        message_received.disconnect(receiver)


class TestCatalog:
    def test_the_name_is_the_wire_format(self) -> None:
        """It appears in ``outbound_webhook.events`` (SPEC §5), so renaming it
        breaks configured integrations in a way no test here would notice."""
        assert EVENT_MESSAGE_RECEIVED == "message.received"
        assert MESSAGING_EVENT_NAMES == ("message.received",)

    def test_an_unknown_event_name_raises(self) -> None:
        """A KeyError on a typo is the point: the alternative is a silent
        no-send that looks exactly like "nobody subscribed"."""
        with pytest.raises(KeyError):
            emit("message.recieved", workspace_id=uuid4(), contact_id=uuid4())


class TestEmission:
    def test_an_inbound_message_emits_once(self, tenancy: Any, connection: Any, received: list) -> None:
        persist_events(connection, [make_event(connection)])
        assert len(received) == 1

    def test_the_payload_carries_ids_and_nothing_else(self, tenancy: Any, connection: Any, received: list) -> None:
        """Contract 7: "payloads carry workspace id, contact id, and
        event-specific ids only (no message bodies)". An outbound webhook
        delivers this to a third-party URL."""
        persist_events(connection, [make_event(connection, text="a secret")])
        payload = received[0]
        conversation = Conversation.objects.for_workspace(tenancy.workspace).get()
        message = Message.objects.for_workspace(tenancy.workspace).get()

        assert payload["event"] == EVENT_MESSAGE_RECEIVED
        assert payload["workspace_id"] == tenancy.workspace.pk
        assert payload["contact_id"] == conversation.contact_id
        assert payload["conversation_id"] == conversation.pk
        assert payload["message_id"] == message.pk
        assert payload["connection_id"] == connection.pk
        assert payload["platform"] == connection.platform
        assert payload["occurred_at"] is not None
        assert "a secret" not in repr(payload)

    def test_a_redelivered_event_does_not_emit_again(self, connection: Any, received: list) -> None:
        event = make_event(connection, event_id="once")
        persist_events(connection, [event])
        persist_events(connection, [event])
        assert len(received) == 1

    @pytest.mark.parametrize("kind", [EventType.REFERRAL, EventType.FOLLOW, EventType.OPT_OUT])
    def test_only_thread_events_emit(self, connection: Any, received: list, kind: str) -> None:
        """The event is named message.received, so it fires when a message was."""
        persist_events(connection, [make_event(connection, kind=kind)])
        assert received == []

    def test_a_receiver_sees_the_row_it_was_told_about(self, tenancy: Any, connection: Any) -> None:
        """Emitted synchronously inside the event's transaction, and last — so a
        receiver that reads the message finds it. ``transaction.on_commit``
        would never run under pytest.mark.django_db at all."""
        seen: list[bool] = []

        def receiver(sender: Any, **kwargs: Any) -> None:
            seen.append(Message.all_objects.filter(pk=kwargs["message_id"]).exists())

        message_received.connect(receiver)
        try:
            persist_events(connection, [make_event(connection)])
        finally:
            message_received.disconnect(receiver)
        assert seen == [True]
