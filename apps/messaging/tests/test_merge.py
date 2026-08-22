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
from apps.messaging.handlers import schedule_send_retry
from apps.messaging.ingest import persist_events
from apps.messaging.models import ContactChannelIdentity, Conversation, Message, MessageStatus
from apps.messaging.tests.conftest import make_connection, make_event
from apps.queueing.models import ActionStatus, ScheduledAction

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


class TestQueuedSendsFollowTheSurvivor:
    def test_a_pending_send_retry_is_retargeted(self, tenancy: Any, connection: Any) -> None:
        """ScheduledAction.contact_id is a plain UUID, not a foreign key, so
        nothing cascades it. Left naming the tombstone, the worker would take
        the *deleted* contact's advisory lock while the handler sent a message
        that now belongs to the survivor — so the send could interleave with
        flow work holding the survivor's lock, which is the one thing SPEC §9.6
        exists to prevent."""
        persist_events(connection, [make_event(connection, user="u1")])
        duplicate = ContactChannelIdentity.objects.for_workspace(tenancy.workspace).get().contact
        conversation = threads(tenancy.workspace, duplicate).get()
        message = Message.objects.create(
            conversation=conversation,
            direction="out",
            source="automation",
            status=MessageStatus.QUEUED,
            idempotency_key="pending",
            send_attempts=1,
        )
        action = schedule_send_retry(message)
        assert action is not None
        assert action.contact_id == duplicate.pk

        primary = create_contact(tenancy.workspace, first_name="Survivor")
        merge_contacts(primary=primary, duplicate=duplicate)

        action.refresh_from_db()
        assert action.contact_id == primary.pk

    def test_a_finished_retry_is_left_alone(self, tenancy: Any, connection: Any) -> None:
        """Only pending work needs its lock moved; a completed action is history."""
        persist_events(connection, [make_event(connection, user="u1")])
        duplicate = ContactChannelIdentity.objects.for_workspace(tenancy.workspace).get().contact
        conversation = threads(tenancy.workspace, duplicate).get()
        message = Message.objects.create(
            conversation=conversation,
            direction="out",
            source="automation",
            status=MessageStatus.SENT,
            idempotency_key="done",
            send_attempts=1,
        )
        action = schedule_send_retry(message)
        assert action is not None
        ScheduledAction.objects.unscoped().filter(pk=action.pk).update(status=ActionStatus.DONE)

        primary = create_contact(tenancy.workspace, first_name="Survivor")
        merge_contacts(primary=primary, duplicate=duplicate)

        action.refresh_from_db()
        assert action.contact_id == duplicate.pk


class TestCollidingIdempotencyKeys:
    def test_two_threads_holding_one_key_still_merge(self, tenancy: Any, connection: Any) -> None:
        """(conversation, idempotency_key) is unique, so folding two threads that
        each hold a message with the same key used to raise out of the bulk
        update and take the whole merge down. History is never dropped; the key
        is cleared instead, because it guards a *future* insert and a thread
        being merged away has none."""
        persist_events(connection, [make_event(connection, user="u1", event_id="e1", text="from A")])
        first = ContactChannelIdentity.objects.for_workspace(tenancy.workspace).get().contact
        persist_events(connection, [make_event(connection, user="u2", event_id="e2", text="from B")])
        second = ContactChannelIdentity.objects.for_workspace(tenancy.workspace).exclude(contact=first).get().contact
        # The same key in both threads — two sends that reused one across
        # contacts, or one provider event persisted to both people.
        for conversation in Conversation.objects.for_workspace(tenancy.workspace):
            Message.objects.for_workspace(tenancy.workspace).filter(conversation=conversation).update(
                idempotency_key="shared"
            )

        merge_contacts(primary=first, duplicate=second)

        surviving = threads(tenancy.workspace, first).get()
        moved = Message.objects.for_workspace(tenancy.workspace).filter(conversation=surviving)
        assert moved.count() == 2
        assert {m.body["blocks"][0]["text"] for m in moved} == {"from A", "from B"}
        # Exactly one kept the key; the other had it cleared to make room.
        assert sorted(m.idempotency_key for m in moved) == ["", "shared"]


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
