"""Merging two contacts moves their messaging rows (``apps.messaging.merge``).

``apps.contacts.services.merge_contacts`` names issue #8 as the owner of this:
without it, the moment these tables exist a merge leaves identities and threads
attached to a tombstone — the duplicate is ``status=deleted``, every read
surface starts from active contacts, and the conversation vanishes from the
inbox while its identity keeps receiving webhooks.
"""

from typing import Any

import pytest

from apps.common.platforms import Platform
from apps.contacts.models import ContactStatus
from apps.contacts.services import create_contact, merge_contacts
from apps.messaging.ingest import persist_events
from apps.messaging.models import ContactChannelIdentity, Conversation, Message
from apps.messaging.tests.conftest import make_connection, make_event

pytestmark = pytest.mark.django_db


def identities(workspace: Any, contact: Any) -> int:
    return ContactChannelIdentity.objects.for_workspace(workspace).filter(contact=contact).count()


def threads(workspace: Any, contact: Any) -> Any:
    return Conversation.objects.for_workspace(workspace).filter(contact=contact)


class TestIdentities:
    def test_they_move_to_the_survivor(self, tenancy: Any, connection: Any) -> None:
        persist_events(connection, [make_event(connection, user="u1")])
        duplicate = ContactChannelIdentity.objects.for_workspace(tenancy.workspace).get().contact
        primary = create_contact(tenancy.workspace, first_name="Survivor")

        merge_contacts(primary=primary, duplicate=duplicate)

        assert identities(tenancy.workspace, primary) == 1
        assert identities(tenancy.workspace, duplicate) == 0

    def test_a_later_event_reaches_the_survivor(self, tenancy: Any, connection: Any) -> None:
        """The point of moving them: the platform keeps sending to the same
        platform_user_id, and those messages must land on the person who is
        still in the CRM."""
        persist_events(connection, [make_event(connection, user="u1", event_id="e1")])
        duplicate = ContactChannelIdentity.objects.for_workspace(tenancy.workspace).get().contact
        primary = create_contact(tenancy.workspace, first_name="Survivor")
        merge_contacts(primary=primary, duplicate=duplicate)

        persist_events(connection, [make_event(connection, user="u1", event_id="e2")])

        assert threads(tenancy.workspace, primary).count() == 1
        assert Message.objects.for_workspace(tenancy.workspace).count() == 2


class TestConversations:
    def test_a_thread_on_a_connection_the_survivor_lacks_simply_moves(self, tenancy: Any, connection: Any) -> None:
        persist_events(connection, [make_event(connection, user="u1")])
        duplicate = ContactChannelIdentity.objects.for_workspace(tenancy.workspace).get().contact
        primary = create_contact(tenancy.workspace, first_name="Survivor")

        merge_contacts(primary=primary, duplicate=duplicate)

        assert threads(tenancy.workspace, primary).count() == 1
        assert threads(tenancy.workspace, duplicate).count() == 0

    def test_two_threads_on_one_connection_fold_into_one(self, tenancy: Any, connection: Any) -> None:
        """``(contact, connection)`` is unique, so the survivor's thread is kept
        and the duplicate's messages move into it. A merge must not lose message
        history, and must not leave one person two threads on one channel."""
        persist_events(connection, [make_event(connection, user="u1", event_id="e1", text="from A")])
        first = ContactChannelIdentity.objects.for_workspace(tenancy.workspace).get().contact
        persist_events(connection, [make_event(connection, user="u2", event_id="e2", text="from B")])
        second = ContactChannelIdentity.objects.for_workspace(tenancy.workspace).exclude(contact=first).get().contact

        merge_contacts(primary=first, duplicate=second)

        assert threads(tenancy.workspace, first).count() == 1
        surviving = threads(tenancy.workspace, first).get()
        texts = {
            message.body["blocks"][0]["text"]
            for message in Message.objects.for_workspace(tenancy.workspace).filter(conversation=surviving)
        }
        assert texts == {"from A", "from B"}

    def test_the_surviving_thread_keeps_the_later_recency(self, tenancy: Any, connection: Any) -> None:
        """The inbox sorts on last_message_at; losing the newer timestamp would
        bury a live conversation."""
        persist_events(connection, [make_event(connection, user="u1", event_id="e1")])
        first = ContactChannelIdentity.objects.for_workspace(tenancy.workspace).get().contact
        older = threads(tenancy.workspace, first).get().last_message_at

        persist_events(connection, [make_event(connection, user="u2", event_id="e2")])
        second = ContactChannelIdentity.objects.for_workspace(tenancy.workspace).exclude(contact=first).get().contact
        newer = threads(tenancy.workspace, second).get().last_message_at

        merge_contacts(primary=first, duplicate=second)

        assert newer > older
        assert threads(tenancy.workspace, first).get().last_message_at == newer

    def test_threads_on_different_connections_both_survive(self, tenancy: Any, connection: Any) -> None:
        sms = make_connection(tenancy.workspace, platform=Platform.SMS, suffix="sms")
        persist_events(connection, [make_event(connection, user="u1", event_id="e1")])
        first = ContactChannelIdentity.objects.for_workspace(tenancy.workspace).get().contact
        persist_events(sms, [make_event(sms, user="+15550101234", event_id="e2")])
        second = ContactChannelIdentity.objects.for_workspace(tenancy.workspace).exclude(contact=first).get().contact

        merge_contacts(primary=first, duplicate=second)

        assert threads(tenancy.workspace, first).count() == 2


class TestTheTombstoneIsClean:
    def test_nothing_messaging_is_left_pointing_at_the_duplicate(self, tenancy: Any, connection: Any) -> None:
        persist_events(connection, [make_event(connection, user="u1")])
        duplicate = ContactChannelIdentity.objects.for_workspace(tenancy.workspace).get().contact
        primary = create_contact(tenancy.workspace, first_name="Survivor")

        merge_contacts(primary=primary, duplicate=duplicate)

        duplicate.refresh_from_db()
        assert duplicate.status == ContactStatus.DELETED
        assert identities(tenancy.workspace, duplicate) == 0
        assert threads(tenancy.workspace, duplicate).count() == 0
