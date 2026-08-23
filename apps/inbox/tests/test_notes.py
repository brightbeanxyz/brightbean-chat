"""Internal notes: stored as messages, never sent to anybody (SPEC §14).

The load-bearing assertion in this file is the negative one — a note makes no
adapter call — and it is made against a real registered adapter with a send log
rather than a mock, so "never reached the send pipeline" means the pipeline
really ran and really did not call out.
"""

from typing import Any

import pytest

from apps.channels.tests.fake_adapter import registered
from apps.common.platforms import Platform
from apps.inbox.selectors import unread_count_for
from apps.messaging.models import Conversation, Message, MessageDirection, MessageSource, MessageStatus

pytestmark = pytest.mark.django_db


def _note(conversation: Conversation) -> Message:
    return Message.objects.for_workspace(conversation.workspace_id).get(conversation=conversation)


class TestANote:
    def test_it_never_calls_an_adapter(
        self, agent_client: Any, url_for: Any, conversation: Conversation, identity: Any
    ) -> None:
        with registered(Platform.TELEGRAM) as adapter:
            response = agent_client.post(
                url_for("note", conversation_id=conversation.pk), {"body": "chase this up tomorrow"}
            )

        assert response.status_code == 204
        assert adapter.sends == [], "an internal note reached the send pipeline"

    def test_it_is_stored_as_a_message_the_spec_way(
        self, agent_client: Any, url_for: Any, conversation: Conversation, identity: Any
    ) -> None:
        """ "stored as message with source=agent, direction out, flag internal"."""
        with registered(Platform.TELEGRAM):
            agent_client.post(url_for("note", conversation_id=conversation.pk), {"body": "chase this up"})

        note = _note(conversation)
        assert note.internal is True
        assert note.direction == MessageDirection.OUT
        assert note.source == MessageSource.AGENT
        assert note.status == MessageStatus.SENT
        assert note.provider_message_id == ""

    def test_it_is_written_even_when_compliance_would_refuse_a_reply(
        self, agent_client: Any, url_for: Any, conversation: Conversation, identity: Any
    ) -> None:
        """A note is a message to the team, so an opted-out contact has no
        bearing on it — the facade skips compliance for the internal path."""
        from django.utils import timezone

        identity.opted_out_at = timezone.now()
        identity.save(update_fields=["opted_out_at", "updated_at"])

        with registered(Platform.TELEGRAM) as adapter:
            agent_client.post(url_for("note", conversation_id=conversation.pk), {"body": "they opted out"})

        assert _note(conversation).status == MessageStatus.SENT
        assert adapter.sends == []

    def test_it_needs_no_identity_at_all(self, agent_client: Any, url_for: Any, conversation: Conversation) -> None:
        with registered(Platform.TELEGRAM) as adapter:
            agent_client.post(url_for("note", conversation_id=conversation.pk), {"body": "no address on file"})

        assert _note(conversation).internal is True
        assert adapter.sends == []

    def test_it_is_visually_distinct_in_the_thread(
        self, agent_client: Any, url_for: Any, conversation: Conversation, identity: Any
    ) -> None:
        with registered(Platform.TELEGRAM):
            agent_client.post(url_for("note", conversation_id=conversation.pk), {"body": "team eyes only"})

        body = agent_client.get(url_for("messages", conversation_id=conversation.pk)).content.decode()

        assert "ib-bubble-note" in body
        assert "Internal note — never sent" in body

    def test_it_never_marks_the_conversation_unread(
        self, tenancy: Any, agent_client: Any, url_for: Any, conversation: Conversation, identity: Any
    ) -> None:
        """Notes are excluded from the contact-visible counts: unread means the
        contact said something, and a note is the team talking to itself."""
        with registered(Platform.TELEGRAM):
            agent_client.post(url_for("note", conversation_id=conversation.pk), {"body": "for later"})

        assert unread_count_for(tenancy.workspace, tenancy.user_for("editor")) == 0

    def test_an_empty_note_is_refused(
        self, agent_client: Any, url_for: Any, conversation: Conversation, identity: Any
    ) -> None:
        with registered(Platform.TELEGRAM):
            response = agent_client.post(url_for("note", conversation_id=conversation.pk), {"body": ""})

        assert response.status_code == 204
        assert not Message.objects.for_workspace(conversation.workspace_id).exists()

    def test_a_viewer_cannot_leave_one(self, viewer_client: Any, url_for: Any, conversation: Conversation) -> None:
        response = viewer_client.post(url_for("note", conversation_id=conversation.pk), {"body": "hello"})

        assert response.status_code == 403
        assert not Message.objects.for_workspace(conversation.workspace_id).exists()
