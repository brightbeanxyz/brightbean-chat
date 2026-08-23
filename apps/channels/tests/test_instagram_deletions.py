"""``message_deletions``: redact the body, keep the row (SPEC §6.3, §19).

    Message deletion webhooks (Instagram): redact stored body.  — SPEC §19
    message_deletions (must be handled: redact message body, keep row with
    status deleted).                                             — SPEC §6.3

Both halves matter and they pull in opposite directions. Deleting the row would
lose the thread's shape and an agent's memory of a conversation that happened;
leaving the body would mean "deleted" was a flag somebody has to remember to
check. Redacting in place is the only answer that is true to both.
"""

import json
from collections.abc import Iterator
from typing import Any

import pytest
from django.test import Client

from apps.channels.models import ChannelConnection
from apps.channels.providers.meta_common import SIGNATURE_HEADER
from apps.channels.tests.instagram_support import at_now, fake_graph, load_delivery, sign
from apps.inbox.rendering import preview_of, render_message
from apps.messaging.models import Message, MessageDirection, MessageSource, MessageStatus
from tests.support import Tenancy

pytestmark = pytest.mark.django_db

WEBHOOK_URL = "/webhooks/instagram/"
MID = "aWdfZG1fMTox"


@pytest.fixture
def real_pipeline() -> Iterator[None]:
    from apps.flows.triggers.pipeline import register_routing
    from apps.messaging.ingest import register_processors

    register_processors()
    register_routing()
    yield


def deliver(client: Client, payload: dict[str, Any]) -> Any:
    body = json.dumps(payload).encode()
    return client.post(
        WEBHOOK_URL,
        data=body,
        content_type="application/json",
        headers={SIGNATURE_HEADER: sign(body)},
    )


@pytest.mark.usefixtures("instagram_app", "real_pipeline")
class TestRedaction:
    def test_an_inbound_message_is_redacted_and_kept(
        self, client: Client, tenancy: Tenancy, instagram_connection: ChannelConnection
    ) -> None:
        with fake_graph():
            deliver(client, at_now(load_delivery("message_text")))

        message = Message.objects.for_workspace(tenancy.workspace).get()
        assert message.body["blocks"][0]["text"] == "Do you ship to Berlin?"
        # The platform's own id is stored, which is the only thing a deletion
        # names the message by.
        assert message.provider_message_id == MID

        with fake_graph():
            deliver(client, at_now(load_delivery("message_deleted")))

        message.refresh_from_db()
        assert message.status == MessageStatus.DELETED
        assert message.body["blocks"] == []
        assert message.body["deleted"] is True
        assert "Berlin" not in json.dumps(message.body)
        # The row survives, with its place in the thread.
        assert Message.objects.for_workspace(tenancy.workspace).count() == 1

    def test_an_outbound_message_is_redacted_too(
        self, client: Client, tenancy: Tenancy, instagram_connection: ChannelConnection
    ) -> None:
        """A contact deletes their own DM as readily as we delete ours, and the
        webhook does not say which — so the match is on the id, either direction.
        """
        from apps.contacts.services import create_contact
        from apps.messaging import services

        contact = create_contact(workspace=tenancy.workspace, first_name="Ada")
        services.upsert_contact_identity(
            contact,
            instagram_connection.platform,
            "6789012345678901",
            source="manual",
            opt_in=True,
            connection=instagram_connection,
        )
        conversation = services.open_conversation(
            workspace=tenancy.workspace, contact=contact, connection=instagram_connection
        )
        sent = Message.objects.create(
            conversation=conversation,
            direction=MessageDirection.OUT,
            source=MessageSource.AGENT,
            status=MessageStatus.SENT,
            provider_message_id=MID,
            body={"blocks": [{"type": "text", "text": "Yes, 5 EUR to Berlin."}]},
        )

        with fake_graph():
            deliver(client, at_now(load_delivery("message_deleted")))

        sent.refresh_from_db()
        assert sent.status == MessageStatus.DELETED
        assert sent.body["blocks"] == []

    def test_a_deletion_naming_nothing_we_stored_is_ignored(
        self, client: Client, tenancy: Tenancy, instagram_connection: ChannelConnection
    ) -> None:
        """Normal rather than exceptional: an account can be connected after a
        conversation started, and platforms redeliver."""
        with fake_graph():
            assert deliver(client, at_now(load_delivery("message_deleted"))).status_code == 200
        assert not Message.objects.for_workspace(tenancy.workspace).exists()

    def test_a_redelivered_deletion_is_a_no_op(
        self, client: Client, tenancy: Tenancy, instagram_connection: ChannelConnection
    ) -> None:
        with fake_graph():
            deliver(client, at_now(load_delivery("message_text")))
            deliver(client, at_now(load_delivery("message_deleted")))
            message = Message.objects.for_workspace(tenancy.workspace).get()
            first = message.updated_at
            # A second delivery with its own event id, so the event log does not
            # absorb it and the idempotency being tested is this module's.
            payload = at_now(load_delivery("message_deleted"))
            payload["entry"][0]["messaging"][0]["message"]["mid"] = MID
            payload["entry"][0]["time"] = payload["entry"][0]["time"] + 1
            deliver(client, payload)

        message.refresh_from_db()
        assert message.status == MessageStatus.DELETED
        assert message.updated_at == first

    def test_a_deletion_does_not_open_the_messaging_window(
        self, client: Client, tenancy: Tenancy, instagram_connection: ChannelConnection
    ) -> None:
        """Unsending a message is not a message. It must not buy 24 more hours."""
        from apps.messaging.models import ContactChannelIdentity

        with fake_graph():
            deliver(client, at_now(load_delivery("message_deleted")))
        assert not ContactChannelIdentity.objects.for_workspace(tenancy.workspace).exists()

    def test_a_deletion_fires_no_trigger(
        self, client: Client, tenancy: Tenancy, instagram_connection: ChannelConnection
    ) -> None:
        from apps.channels.events import EventType
        from apps.flows.triggers.pipeline import ROUTABLE_EVENTS

        assert EventType.MESSAGE_DELETED not in ROUTABLE_EVENTS


class TestTheTombstone:
    def test_a_redacted_message_renders_as_a_tombstone(self, tenancy: Tenancy) -> None:
        """Not silence. A reader looking at a hole in a thread with nothing to
        explain it is the failure ``apps.inbox.rendering`` exists to prevent."""
        from apps.channels.models import ChannelConnection as Connection
        from apps.common.platforms import Platform
        from apps.contacts.services import create_contact
        from apps.messaging import services
        from apps.messaging.ingest import REDACTED_BODY

        connection = Connection.objects.create(
            workspace=tenancy.workspace,
            platform=Platform.INSTAGRAM,
            display_name="@x",
            external_id="ig-tombstone",
        )
        contact = create_contact(workspace=tenancy.workspace, first_name="Ada")
        conversation = services.open_conversation(workspace=tenancy.workspace, contact=contact, connection=connection)
        message = Message.objects.create(
            conversation=conversation,
            direction=MessageDirection.IN,
            body=dict(REDACTED_BODY),
            status=MessageStatus.DELETED,
        )

        from apps.inbox.rendering import DELETED_PREVIEW, DELETED_REASON, Tombstone

        rendered = render_message(message)
        assert rendered.is_deleted is True
        (part,) = rendered.parts
        assert isinstance(part, Tombstone)
        assert part.reason == DELETED_REASON
        assert preview_of(message) == DELETED_PREVIEW
