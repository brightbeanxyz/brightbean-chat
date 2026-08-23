"""The per-member read cursor, and the unread indication built on it (SPEC §14).

``apps.messaging`` has no notion of who has read what, so this is the one piece
of state the inbox owns. Two properties matter: the cursor only moves forward,
and "unread" means the *contact* said something — never the agent's own reply
and never an internal note.
"""

from datetime import timedelta
from typing import Any

import pytest
from django.utils import timezone

from apps.contacts.models import Contact
from apps.inbox.models import ConversationRead
from apps.inbox.selectors import unread_count_for
from apps.inbox.services import mark_read
from apps.messaging.models import Conversation
from apps.messaging.services import close_conversation, open_conversation
from apps.messaging.tests.conftest import make_connection

pytestmark = pytest.mark.django_db


class TestTheCursor:
    def test_opening_a_thread_marks_it_read(
        self, tenancy: Any, agent_client: Any, url_for: Any, conversation: Conversation, inbound: Any
    ) -> None:
        inbound("hello")
        agent = tenancy.user_for("agent")
        assert unread_count_for(tenancy.workspace, agent) == 1

        agent_client.get(url_for("thread", conversation_id=conversation.pk))

        assert unread_count_for(tenancy.workspace, agent) == 0

    def test_it_is_per_member(
        self, tenancy: Any, agent_client: Any, url_for: Any, conversation: Conversation, inbound: Any
    ) -> None:
        """One agent reading a thread must not clear it for the rest of the team."""
        inbound("hello")

        agent_client.get(url_for("thread", conversation_id=conversation.pk))

        assert unread_count_for(tenancy.workspace, tenancy.user_for("agent")) == 0
        assert unread_count_for(tenancy.workspace, tenancy.user_for("editor")) == 1

    def test_it_never_moves_backwards(self, tenancy: Any, conversation: Conversation) -> None:
        """Two tabs polling one thread land here out of order often enough to
        matter, and a cursor that can rewind makes a read conversation reappear
        as unread with nothing having happened."""
        agent = tenancy.user_for("agent")
        later = timezone.now()
        earlier = later - timedelta(minutes=10)

        mark_read(conversation, agent, at=later)
        mark_read(conversation, agent, at=earlier)

        row = ConversationRead.objects.for_workspace(tenancy.workspace).get(conversation=conversation)
        assert row.last_read_at == later

    def test_two_overlapping_requests_cannot_drag_it_backwards(self, tenancy: Any, conversation: Conversation) -> None:
        """The interleaving a read-then-decide-in-Python version allows.

        Both requests read the same stored value and both conclude they are
        newer; the one holding the *earlier* timestamp commits last and wins.
        The comparison is a condition on the UPDATE now, so the row arbitrates
        and the loser simply matches nothing.
        """
        agent = tenancy.user_for("agent")
        start = timezone.now()
        later = start + timedelta(minutes=5)
        earlier = start + timedelta(minutes=1)
        mark_read(conversation, agent, at=start)

        # Both hold the same stale instance, which is what overlapping requests
        # have — each loaded the row before either wrote.
        stale = ConversationRead.objects.for_workspace(tenancy.workspace).get(conversation=conversation)
        assert stale.last_read_at == start
        mark_read(conversation, agent, at=later)
        mark_read(conversation, agent, at=earlier)

        stale.refresh_from_db()
        assert stale.last_read_at == later

    def test_it_derives_its_workspace_from_the_conversation(self, tenancy: Any, conversation: Conversation) -> None:
        """Same discipline as messaging.Message: the tenant column is not
        something a caller gets to supply."""
        row = mark_read(conversation, tenancy.user_for("agent"), at=timezone.now())

        assert row.workspace_id == conversation.workspace_id

    def test_a_new_inbound_message_makes_it_unread_again(
        self, tenancy: Any, agent_client: Any, url_for: Any, conversation: Conversation, inbound: Any
    ) -> None:
        inbound("hello")
        agent_client.get(url_for("thread", conversation_id=conversation.pk))

        inbound("still there?")

        assert unread_count_for(tenancy.workspace, tenancy.user_for("agent")) == 1

    def test_polling_a_quiet_thread_does_not_rewrite_the_row(
        self, tenancy: Any, agent_client: Any, url_for: Any, conversation: Conversation, inbound: Any
    ) -> None:
        """The cursor advances on a 200 and never on a 304. Rewriting it three
        times a second per open tab would bust the list's own ETag on every
        cycle — a conditional GET acting as a change notification for itself."""
        inbound("hello")
        url = url_for("messages", conversation_id=conversation.pk)
        etag = agent_client.get(url).headers["ETag"]
        row = ConversationRead.objects.for_workspace(tenancy.workspace).get(conversation=conversation)
        first_write = row.updated_at

        assert agent_client.get(url, headers={"If-None-Match": etag}).status_code == 304

        row.refresh_from_db()
        assert row.updated_at == first_write


class TestTheUnreadCount:
    def test_it_counts_open_conversations_only(self, tenancy: Any, conversation: Conversation, inbound: Any) -> None:
        """A done thread with an unread message is not work waiting on anybody;
        reopening it is what puts it back in the queue."""
        inbound("hello")
        close_conversation(conversation)

        assert unread_count_for(tenancy.workspace, tenancy.user_for("agent")) == 0

    def test_it_never_leaks_across_workspaces(
        self, tenancy: Any, other_tenancy: Any, conversation: Conversation, inbound: Any
    ) -> None:
        inbound("hello")
        theirs = open_conversation(
            workspace=other_tenancy.workspace,
            contact=Contact.objects.create(workspace=other_tenancy.workspace, first_name="Grace"),
            connection=make_connection(other_tenancy.workspace),
        )
        theirs.last_message_at = timezone.now()
        theirs.save(update_fields=["last_message_at", "updated_at"])

        assert unread_count_for(other_tenancy.workspace, tenancy.user_for("agent")) == 0

    def test_the_sidebar_badge_reflects_it(
        self, agent_client: Any, url_for: Any, conversation: Conversation, inbound: Any
    ) -> None:
        """The nav row's badge was wired and hard-zeroed with a TODO(L4-D);
        this is that TODO's assertion.

        On the visible pill, not on the id. Issue #67 made the slot render
        unconditionally — empty and hidden at zero, so a polled out-of-band swap
        always has a target — which means the id alone would now pass whatever
        the count was. The `sidebar-badge` class is on the non-zero branch only.
        """
        inbound("hello")

        page = agent_client.get(url_for("list")).content.decode()

        assert '<span class="sidebar-badge" id="nav-badge-inbox">1</span>' in page

    def test_the_badge_is_an_empty_slot_when_nothing_is_waiting(
        self, agent_client: Any, url_for: Any, conversation: Conversation
    ) -> None:
        """Zero shows nothing, but still leaves the element issue #67's swap
        needs — the inbox row uses the same `nav-badge-<key>` convention, so it
        inherits that fix and has to keep holding up its end of it."""
        page = agent_client.get(url_for("list")).content.decode()

        assert '<span id="nav-badge-inbox" hidden></span>' in page
        assert 'class="sidebar-badge" id="nav-badge-inbox"' not in page

    def test_it_saturates_rather_than_scanning_the_whole_workspace(self, tenancy: Any) -> None:
        """This runs in the shell's context processor, so it is on the critical
        path of every authenticated page in the product — and it is a correlated
        EXISTS per open conversation, not the single indexed count() the
        notification badge beside it does. The slice lets Postgres stop once it
        has enough rows to fill a two-digit badge."""
        from apps.contacts.models import Contact
        from apps.inbox.selectors import UNREAD_BADGE_CAP
        from apps.messaging.models import Message, MessageDirection, MessageStatus

        connection = make_connection(tenancy.workspace, suffix="cap")
        for index in range(UNREAD_BADGE_CAP + 5):
            contact = Contact.objects.create(workspace=tenancy.workspace, first_name=f"P{index}")
            thread = open_conversation(workspace=tenancy.workspace, contact=contact, connection=connection)
            Message.objects.create(
                conversation=thread,
                direction=MessageDirection.IN,
                status=MessageStatus.DELIVERED,
                body={"blocks": [{"type": "text", "text": "hi"}]},
                idempotency_key=f"in:cap:{index}",
            )

        assert unread_count_for(tenancy.workspace, tenancy.user_for("agent")) == UNREAD_BADGE_CAP

    def test_an_anonymous_render_never_reaches_the_query(self, client: Any) -> None:
        """The style guide at /ui/ calls the nav context processor directly for
        visitors with no session, and its docstring promises it "reads no
        database and no session" — so the badge is guarded on authentication,
        not only on a resolved workspace."""
        response = client.get("/ui/")

        assert response.status_code == 200

    def test_a_member_without_use_inbox_costs_no_query(self, tenancy: Any, client_for: Any) -> None:
        """Every authenticated page render pays for this count, so it is skipped
        for somebody who cannot see the row it sits on. Owners hold every key,
        so the case under test is an org member with no workspace membership at
        all — for whom there is no workspace to count in either."""
        from apps.accounts.models import User
        from apps.members.models import OrgMembership
        from apps.members.roles import OrgRole

        outsider = User.objects.create_user(email="nobody@acme.test", password="x")  # noqa: S106
        OrgMembership.objects.create(user=outsider, organization=tenancy.organization, org_role=OrgRole.MEMBER)

        response = client_for(outsider).get("/organization/workspaces/")

        assert response.status_code == 200
        assert 'id="nav-badge-inbox"' not in response.content.decode()
