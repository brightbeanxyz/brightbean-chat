"""The conversation list and the thread: what renders, filtered and in order."""

from datetime import timedelta
from typing import Any

import pytest
from django.utils import timezone

from apps.contacts.models import Contact
from apps.inbox.selectors import PAGE_SIZE
from apps.messaging.models import Conversation, ConversationState, Message, MessageDirection, MessageStatus
from apps.messaging.services import assign_conversation, close_conversation, open_conversation
from apps.messaging.tests.conftest import make_connection

pytestmark = pytest.mark.django_db


def _other_conversation(tenancy: Any, name: str, *, connection: Any = None) -> Conversation:
    """A second thread. The suffix is not decoration: SPEC §5's unique on
    (platform, external_id) is deployment-wide, and make_connection's default
    namespaces on the workspace — so two calls for one workspace collide."""
    contact = Contact.objects.create(workspace=tenancy.workspace, first_name=name)
    return open_conversation(
        workspace=tenancy.workspace,
        contact=contact,
        connection=connection or make_connection(tenancy.workspace, suffix=f"{tenancy.slug}-{name}"),
    )


class TestTheList:
    def test_it_renders_the_workspace_conversations(
        self, agent_client: Any, url_for: Any, conversation: Conversation, inbound: Any
    ) -> None:
        inbound("hello there")

        response = agent_client.get(url_for("list"))

        assert response.status_code == 200
        body = response.content.decode()
        assert "Ada Lovelace" in body
        assert "hello there" in body

    def test_it_never_shows_another_tenants_conversation(
        self, agent_client: Any, url_for: Any, other_tenancy: Any
    ) -> None:
        """The scoped manager is the mechanism; this is the assertion that the
        page actually goes through it."""
        stranger = _other_conversation(other_tenancy, "Grace")

        body = agent_client.get(url_for("list")).content.decode()

        assert "Grace" not in body
        assert str(stranger.pk) not in body

    def test_it_sorts_by_last_message_at(
        self, tenancy: Any, agent_client: Any, url_for: Any, conversation: Conversation, inbound: Any
    ) -> None:
        inbound("older")
        newer = _other_conversation(tenancy, "Zara")
        newer.last_message_at = timezone.now() + timedelta(minutes=5)
        newer.save(update_fields=["last_message_at", "updated_at"])

        body = agent_client.get(url_for("rows")).content.decode()

        assert body.index("Zara") < body.index("Ada")

    def test_the_state_filter_narrows_it(
        self, tenancy: Any, agent_client: Any, url_for: Any, conversation: Conversation
    ) -> None:
        done = _other_conversation(tenancy, "Closed")
        close_conversation(done)

        open_only = agent_client.get(url_for("rows"), {"state": ConversationState.OPEN}).content.decode()
        done_only = agent_client.get(url_for("rows"), {"state": ConversationState.DONE}).content.decode()

        assert "Ada" in open_only and "Closed" not in open_only
        assert "Closed" in done_only and "Ada" not in done_only

    def test_the_channel_filter_narrows_it(
        self, tenancy: Any, agent_client: Any, url_for: Any, conversation: Conversation
    ) -> None:
        second = make_connection(tenancy.workspace, suffix="second")
        _other_conversation(tenancy, "Elsewhere", connection=second)

        body = agent_client.get(url_for("rows"), {"connection": str(second.pk)}).content.decode()

        assert "Elsewhere" in body
        assert "Ada" not in body

    def test_the_assignee_filter_offers_me_and_unassigned(
        self, tenancy: Any, agent_client: Any, url_for: Any, conversation: Conversation
    ) -> None:
        mine = _other_conversation(tenancy, "Mine")
        assign_conversation(mine, tenancy.user_for("agent"))

        assigned = agent_client.get(url_for("rows"), {"assignee": "me"}).content.decode()
        unassigned = agent_client.get(url_for("rows"), {"assignee": "unassigned"}).content.decode()

        assert "Mine" in assigned and "Ada" not in assigned
        assert "Ada" in unassigned and "Mine" not in unassigned

    def test_an_unparseable_assignee_filter_is_not_a_500(
        self, agent_client: Any, url_for: Any, conversation: Conversation
    ) -> None:
        """The value is a query-string fragment, so a bookmark must not be able
        to hand a UUID column something that is not one."""
        response = agent_client.get(url_for("rows"), {"assignee": "../../etc/passwd"})

        assert response.status_code == 200
        assert "Ada" not in response.content.decode()


class TestTheThread:
    def test_it_renders_history_oldest_first(
        self, agent_client: Any, url_for: Any, conversation: Conversation, inbound: Any, outbound: Any
    ) -> None:
        inbound("msg-alpha")
        outbound("msg-omega")

        # The pane alone, not the whole page: the conversation list renders
        # first and its preview line carries the *newest* message, so a
        # full-page comparison would be measuring the rail's ordering rather
        # than the thread's. Distinctive tokens for a related reason — the page
        # also carries an inline script whose comments are ordinary English.
        body = agent_client.get(
            url_for("thread", conversation_id=conversation.pk), headers={"HX-Request": "true"}
        ).content.decode()

        assert body.index("msg-alpha") < body.index("msg-omega")

    def test_a_deep_link_renders_the_whole_page_and_htmx_gets_the_pane(
        self, agent_client: Any, url_for: Any, conversation: Conversation
    ) -> None:
        url = url_for("thread", conversation_id=conversation.pk)

        page = agent_client.get(url).content.decode()
        pane = agent_client.get(url, headers={"HX-Request": "true"}).content.decode()

        assert "<html" in page
        assert "<html" not in pane
        assert "ib-thread" in pane

    def test_another_tenants_conversation_is_a_404_not_a_403(
        self, agent_client: Any, url_for: Any, other_tenancy: Any
    ) -> None:
        stranger = _other_conversation(other_tenancy, "Grace")

        response = agent_client.get(url_for("thread", conversation_id=stranger.pk))

        assert response.status_code == 404

    def test_history_paginates_upward(
        self, tenancy: Any, agent_client: Any, url_for: Any, conversation: Conversation
    ) -> None:
        """A page plus one, so the "load earlier" affordance has to appear."""
        for index in range(PAGE_SIZE + 5):
            Message.objects.create(
                conversation=conversation,
                direction=MessageDirection.IN,
                status=MessageStatus.DELIVERED,
                body={"blocks": [{"type": "text", "text": f"msg-{index}"}]},
                idempotency_key=f"in:{index}",
            )

        first = agent_client.get(url_for("messages", conversation_id=conversation.pk)).content.decode()

        assert "msg-54" in first
        assert "msg-0" not in first
        assert "Load earlier messages" in first

    def test_the_earlier_page_reaches_the_start(
        self, agent_client: Any, url_for: Any, conversation: Conversation
    ) -> None:
        for index in range(PAGE_SIZE + 5):
            Message.objects.create(
                conversation=conversation,
                direction=MessageDirection.IN,
                status=MessageStatus.DELIVERED,
                body={"blocks": [{"type": "text", "text": f"msg-{index}"}]},
                idempotency_key=f"in:{index}",
            )
        oldest_shown = Message.objects.for_workspace(conversation.workspace_id).order_by("-created_at")[PAGE_SIZE - 1]

        earlier = agent_client.get(
            url_for("messages", conversation_id=conversation.pk),
            {"before": oldest_shown.created_at.isoformat()},
        ).content.decode()

        assert "msg-0" in earlier

    def test_a_junk_cursor_falls_back_to_the_newest_page(
        self, agent_client: Any, url_for: Any, conversation: Conversation, inbound: Any
    ) -> None:
        inbound("newest")

        response = agent_client.get(url_for("messages", conversation_id=conversation.pk), {"before": "not-a-timestamp"})

        assert response.status_code == 200
        assert "newest" in response.content.decode()
