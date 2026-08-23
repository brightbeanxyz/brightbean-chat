"""SPEC §14's polling contract, which is an acceptance criterion of issue #14.

    Polling with 100 conversations: unchanged polls return 304 with no body;
    changes appear within one 3 s cycle.

The 3 s is markup (``hx-trigger="every 3s"``), so what is testable here is the
other half: an unchanged poll costs nothing and a changed one is visible on the
very next request.
"""

from datetime import timedelta
from typing import Any

import pytest
from django.utils import timezone

from apps.contacts.models import Contact
from apps.messaging.codes import Denial, describe
from apps.messaging.models import Conversation, Message, MessageDirection, MessageSource, MessageStatus
from apps.messaging.services import assign_conversation, close_conversation, open_conversation
from apps.messaging.tests.conftest import make_connection

pytestmark = pytest.mark.django_db


def _poll(client: Any, url: str, etag: str | None = None, **params: Any) -> Any:
    headers = {"If-None-Match": etag} if etag else {}
    return client.get(url, params, headers=headers)


class TestTheConversationList:
    def test_an_unchanged_poll_is_a_304_with_no_body(
        self, agent_client: Any, url_for: Any, conversation: Conversation, inbound: Any
    ) -> None:
        inbound("hello")
        url = url_for("rows")
        first = _poll(agent_client, url)

        second = _poll(agent_client, url, first.headers["ETag"])

        assert first.status_code == 200
        assert second.status_code == 304
        assert second.content == b""

    def test_it_holds_with_a_hundred_conversations(
        self, tenancy: Any, agent_client: Any, url_for: Any, django_assert_num_queries: Any
    ) -> None:
        """The acceptance number, and the point of the count() half of the
        version token: a hundred rows is where a full re-render every three
        seconds per open tab would start to cost something."""
        connection = make_connection(tenancy.workspace)
        for index in range(100):
            contact = Contact.objects.create(workspace=tenancy.workspace, first_name=f"Person{index}")
            open_conversation(workspace=tenancy.workspace, contact=contact, connection=connection)
        url = url_for("rows")

        first = _poll(agent_client, url)
        second = _poll(agent_client, url, first.headers["ETag"])

        assert first.status_code == 200
        assert second.status_code == 304
        assert second.content == b""
        assert len(first.content) > 1000

    def test_a_new_message_shows_up_on_the_next_poll(
        self, agent_client: Any, url_for: Any, conversation: Conversation, inbound: Any
    ) -> None:
        url = url_for("rows")
        etag = _poll(agent_client, url).headers["ETag"]

        inbound("brand new")
        after = _poll(agent_client, url, etag)

        assert after.status_code == 200
        assert "brand new" in after.content.decode()

    def test_assigning_and_closing_both_bust_the_tag(
        self, tenancy: Any, agent_client: Any, url_for: Any, conversation: Conversation
    ) -> None:
        """The list shows state and assignee, so neither may be invisible to the
        poll — and neither touches a message row."""
        url = url_for("rows")

        etag = _poll(agent_client, url).headers["ETag"]
        assign_conversation(conversation, tenancy.user_for("agent"))
        assert _poll(agent_client, url, etag).status_code == 200

        etag = _poll(agent_client, url).headers["ETag"]
        close_conversation(conversation)
        assert _poll(agent_client, url, etag).status_code == 200

    def test_a_deleted_conversation_busts_the_tag(
        self, tenancy: Any, agent_client: Any, url_for: Any, conversation: Conversation
    ) -> None:
        """Why the token is (max updated_at, count) and not max updated_at.

        Deleting the most recently touched row walks the maximum *backwards* to
        a value the client is already holding, so a bare Max would answer 304 to
        the change that removed it.
        """
        contact = Contact.objects.create(workspace=tenancy.workspace, first_name="Doomed")
        doomed = open_conversation(
            workspace=tenancy.workspace,
            contact=contact,
            connection=make_connection(tenancy.workspace, suffix="doomed"),
        )
        url = url_for("rows")
        etag = _poll(agent_client, url).headers["ETag"]

        doomed.delete()

        assert _poll(agent_client, url, etag).status_code == 200

    def test_different_filters_never_share_a_tag(
        self, agent_client: Any, url_for: Any, conversation: Conversation
    ) -> None:
        url = url_for("rows")

        open_tag = _poll(agent_client, url, state="open").headers["ETag"]
        done_tag = _poll(agent_client, url, state="done").headers["ETag"]

        assert open_tag != done_tag

    def test_two_members_never_share_a_tag(
        self, tenancy: Any, client_for: Any, url_for: Any, conversation: Conversation, inbound: Any
    ) -> None:
        """The unread dot is per member, so the markup is too."""
        inbound("hello")
        url = url_for("rows")

        agent_tag = _poll(client_for(tenancy.user_for("agent")), url).headers["ETag"]
        editor_tag = _poll(client_for(tenancy.user_for("editor")), url).headers["ETag"]

        assert agent_tag != editor_tag

    def test_reading_a_thread_busts_the_readers_own_tag(
        self, agent_client: Any, url_for: Any, conversation: Conversation, inbound: Any
    ) -> None:
        """Otherwise the unread dot would stay lit in every other tab until
        something unrelated happened to the conversation."""
        inbound("hello")
        url = url_for("rows")
        etag = _poll(agent_client, url).headers["ETag"]

        agent_client.get(url_for("thread", conversation_id=conversation.pk))

        assert _poll(agent_client, url, etag).status_code == 200


class TestTheThread:
    def test_an_unchanged_poll_is_a_304_with_no_body(
        self, agent_client: Any, url_for: Any, conversation: Conversation, inbound: Any
    ) -> None:
        inbound("hello")
        url = url_for("messages", conversation_id=conversation.pk)

        first = _poll(agent_client, url)
        second = _poll(agent_client, url, first.headers["ETag"])

        assert first.status_code == 200
        assert second.status_code == 304
        assert second.content == b""

    def test_a_new_message_shows_up_on_the_next_poll(
        self, agent_client: Any, url_for: Any, conversation: Conversation, inbound: Any
    ) -> None:
        url = url_for("messages", conversation_id=conversation.pk)
        etag = _poll(agent_client, url).headers["ETag"]

        inbound("just arrived")
        after = _poll(agent_client, url, etag)

        assert after.status_code == 200
        assert "just arrived" in after.content.decode()

    def test_a_pause_busts_the_tag_without_any_message_changing(
        self, agent_client: Any, url_for: Any, conversation: Conversation
    ) -> None:
        """The banner renders inside the polled region and nothing else moved."""
        from apps.messaging.services import pause_automation

        url = url_for("messages", conversation_id=conversation.pk)
        etag = _poll(agent_client, url).headers["ETag"]

        pause_automation(conversation, timezone.now() + timedelta(minutes=30))

        assert _poll(agent_client, url, etag).status_code == 200

    def test_a_delivery_receipt_busts_the_tag(
        self, agent_client: Any, url_for: Any, conversation: Conversation, outbound: Any
    ) -> None:
        """Ticks are rendered from status, so a receipt has to be visible even
        though no row was added — which is what the max(updated_at) half is for."""
        message = outbound("sent already", status=MessageStatus.SENT)
        url = url_for("messages", conversation_id=conversation.pk)
        etag = _poll(agent_client, url).headers["ETag"]

        message.status = MessageStatus.READ
        message.save(update_fields=["status", "updated_at"])

        assert _poll(agent_client, url, etag).status_code == 200

    def test_a_pause_lapsing_busts_the_tag_even_though_nothing_was_written(
        self, agent_client: Any, url_for: Any, conversation: Conversation
    ) -> None:
        """A pause ends by the clock, not by a write. With a token built only
        from row state the poll answered 304 for ever afterwards, leaving the
        banner insisting automation was paused with nothing able to clear it."""
        from apps.messaging.services import pause_automation

        url = url_for("messages", conversation_id=conversation.pk)
        pause_automation(conversation, timezone.now() + timedelta(minutes=30))
        etag = _poll(agent_client, url).headers["ETag"]
        assert "ib-banner-paused" in _poll(agent_client, url).content.decode()

        # Nothing writes when a pause expires, so the row is moved back in time
        # to stand in for the clock moving forward.
        pause_automation(conversation, timezone.now() - timedelta(seconds=1))
        after = _poll(agent_client, url, etag)

        assert after.status_code == 200
        assert "ib-banner-paused" not in after.content.decode()

    def test_an_opt_out_busts_the_tag_even_though_no_message_moved(
        self, agent_client: Any, url_for: Any, conversation: Conversation, identity: Any
    ) -> None:
        """`_apply_opt_out` writes only the identity — an opt_out event creates
        no message row and never touches the conversation. The compliance notice
        renders inside this region, so without the decision in the token an
        agent went on being told they could reply to somebody who had left."""
        url = url_for("messages", conversation_id=conversation.pk)
        etag = _poll(agent_client, url).headers["ETag"]

        identity.opted_out_at = timezone.now()
        identity.save(update_fields=["opted_out_at", "updated_at"])
        after = _poll(agent_client, url, etag)

        assert after.status_code == 200
        assert describe(Denial.OPTED_OUT.value) in after.content.decode()

    def test_the_banner_carries_no_countdown_it_cannot_keep_current(
        self, agent_client: Any, url_for: Any, conversation: Conversation
    ) -> None:
        """The token tracks *whether* the pause is live, so the banner appears
        and disappears on time — but a "29 minutes left" inside it would go
        wrong on its own while the markup stayed byte-identical."""
        from apps.messaging.services import pause_automation

        pause_automation(conversation, timezone.now() + timedelta(minutes=30))

        body = _poll(agent_client, url_for("messages", conversation_id=conversation.pk)).content.decode()

        assert "ib-banner-paused" in body
        assert "left" not in body.split("Flows will not reply")[0]

    def test_the_window_is_part_of_the_tag(
        self, agent_client: Any, url_for: Any, conversation: Conversation, inbound: Any
    ) -> None:
        """Two window sizes render different history, so they cannot share a tag
        — otherwise widening it would answer 304 and show nothing new."""
        inbound("hello")
        url = url_for("messages", conversation_id=conversation.pk)

        one = _poll(agent_client, url, limit=50).headers["ETag"]
        two = _poll(agent_client, url, limit=100).headers["ETag"]

        assert one != two

    def test_roles_never_share_a_tag(
        self, tenancy: Any, client_for: Any, url_for: Any, conversation: Conversation
    ) -> None:
        """A Viewer's pane has no controls in it, so it is not the same markup."""
        url = url_for("messages", conversation_id=conversation.pk)

        agent_tag = _poll(client_for(tenancy.user_for("agent")), url).headers["ETag"]
        viewer_tag = _poll(client_for(tenancy.user_for("viewer")), url).headers["ETag"]

        assert agent_tag != viewer_tag


class TestWhatTheHeadersPromise:
    def test_polled_responses_are_never_stored_by_the_browser_cache(
        self, agent_client: Any, url_for: Any, conversation: Conversation
    ) -> None:
        """Revalidation here is driven by JavaScript holding the last tag. With
        the HTTP cache also in play a 304 on the wire comes back to the caller
        as a 200 from cache, and "did anything change?" is lost on the way."""
        response = _poll(agent_client, url_for("rows"))

        assert response.headers["Cache-Control"] == "no-store"

    def test_the_tag_is_weak(self, agent_client: Any, url_for: Any, conversation: Conversation) -> None:
        """These responses are semantically equivalent, not byte-identical: a
        relative timestamp inside the markup moves on its own."""
        response = _poll(agent_client, url_for("rows"))

        assert response.headers["ETag"].startswith('W/"')

    def test_a_stale_tag_is_answered_in_full(self, agent_client: Any, url_for: Any, conversation: Conversation) -> None:
        response = _poll(agent_client, url_for("rows"), 'W/"0000000000000000"')

        assert response.status_code == 200
        assert response.content


class TestInternalNotesAndTheCounts:
    def test_a_note_never_marks_a_conversation_unread(
        self, tenancy: Any, agent_client: Any, url_for: Any, conversation: Conversation
    ) -> None:
        """ "Excluded from contact-visible counts": unread means the *contact*
        said something, so an outbound row — note or reply — cannot light it."""
        from apps.inbox.selectors import unread_count_for

        Message.objects.create(
            conversation=conversation,
            direction=MessageDirection.OUT,
            source=MessageSource.AGENT,
            status=MessageStatus.SENT,
            internal=True,
            body={"blocks": [{"type": "text", "text": "chase this up"}]},
            idempotency_key="note:1",
        )

        assert unread_count_for(tenancy.workspace, tenancy.user_for("agent")) == 0

    def test_an_agent_reply_never_marks_it_unread_either(
        self, tenancy: Any, conversation: Conversation, outbound: Any
    ) -> None:
        from apps.inbox.selectors import unread_count_for

        outbound("answered")

        assert unread_count_for(tenancy.workspace, tenancy.user_for("agent")) == 0

    def test_an_inbound_message_does(self, tenancy: Any, conversation: Conversation, inbound: Any) -> None:
        from apps.inbox.selectors import unread_count_for

        inbound("are you there?")

        assert unread_count_for(tenancy.workspace, tenancy.user_for("agent")) == 1
