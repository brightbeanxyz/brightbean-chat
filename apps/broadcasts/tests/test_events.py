"""``broadcast.finished`` — contract 7's last unfilled slot.

The Layer-6 gate asks for one thing here: the event must reach an L5-F outbound
webhook subscriber **with no edit to apps/api/**. That works because
``apps.api.events.discover_catalog`` walks the installed apps looking for an
``EVENT_CATALOG``, so shipping ``apps/broadcasts/events.py`` was the whole
integration. These tests assert the delivery, the payload's shape, and the one
property a fan-out event has to have: it fires exactly once.
"""

import pytest

from apps.api.delivery import ACTION_TYPE
from apps.api.models import OutboundWebhook
from apps.broadcasts import handlers, services
from apps.broadcasts.events import EVENT_BROADCAST_FINISHED
from apps.broadcasts.models import BroadcastStatus
from apps.queueing.models import ActionStatus, ActionType, ScheduledAction


@pytest.fixture
def webhook(tenancy):
    endpoint = OutboundWebhook(
        workspace=tenancy.workspace,
        url="https://receiver.example.com/hooks",
        events=[EVENT_BROADCAST_FINISHED],
    )
    endpoint.rotate_secret()
    endpoint.save()
    return endpoint


def _run_to_completion(workspace, broadcast):
    services.schedule_broadcast(broadcast)
    fanout = ScheduledAction.objects.for_workspace(workspace).filter(type=ActionType.BROADCAST_FANOUT).get()
    handlers.handle_broadcast_fanout(fanout.payload, fanout)
    # Marked done the way the worker would. A fanout row left ``pending`` is work
    # still owed, and ``services.fanout_outstanding`` reads it as such — which is
    # the whole point of that guard, so a helper that skipped this would be
    # testing against a state the product never reaches.
    ScheduledAction.objects.for_workspace(workspace).filter(pk=fanout.pk).update(status=ActionStatus.DONE)
    for action in ScheduledAction.objects.for_workspace(workspace).filter(type=ActionType.BROADCAST_SEND):
        handlers.handle_broadcast_send(action.payload, action)
    broadcast.refresh_from_db()


def _deliveries(workspace):
    return ScheduledAction.objects.for_workspace(workspace).filter(type=ACTION_TYPE)


@pytest.mark.django_db
class TestDelivery:
    def test_a_finished_broadcast_reaches_a_subscriber(
        self, tenancy, webhook, make_contacts, make_broadcast, connection, adapter_for
    ):
        make_contacts(2, connection=connection)
        broadcast = make_broadcast(connection=connection)

        with adapter_for(connection.platform):
            _run_to_completion(tenancy.workspace, broadcast)

        assert broadcast.status == BroadcastStatus.SENT
        delivery = _deliveries(tenancy.workspace).get()
        assert delivery.payload["event"] == EVENT_BROADCAST_FINISHED
        assert delivery.payload["webhook_id"] == str(webhook.pk)

    def test_the_payload_carries_ids_only(
        self, tenancy, webhook, make_contacts, make_broadcast, connection, adapter_for
    ):
        """Contract 7: "workspace id, contact id, and event-specific ids only".

        No counts, no audience, no message content — a subscriber that wants the
        numbers fetches them through the API with its own credentials.
        """
        make_contacts(1, connection=connection)
        broadcast = make_broadcast(connection=connection)

        with adapter_for(connection.platform):
            _run_to_completion(tenancy.workspace, broadcast)

        assert _deliveries(tenancy.workspace).get().payload["data"] == {"broadcast_id": str(broadcast.pk)}

    def test_it_fires_exactly_once_however_many_recipients_settle(
        self, tenancy, webhook, make_contacts, make_broadcast, connection, adapter_for
    ):
        """Several handlers can find the queue empty at the same instant.

        The transition is a conditional UPDATE rather than a read-then-write, so
        exactly one of them may announce — a subscriber receiving this twice
        would double-count in somebody's CRM.
        """
        make_contacts(5, connection=connection)
        broadcast = make_broadcast(connection=connection)

        with adapter_for(connection.platform):
            _run_to_completion(tenancy.workspace, broadcast)
            # And again, the way zombie recovery would re-run the last sends.
            for action in ScheduledAction.objects.for_workspace(tenancy.workspace).filter(
                type=ActionType.BROADCAST_SEND
            ):
                handlers.handle_broadcast_send(action.payload, action)

        assert _deliveries(tenancy.workspace).count() == 1

    def test_a_cancelled_broadcast_does_not_announce_itself_finished(
        self, tenancy, webhook, make_contacts, make_broadcast, connection
    ):
        """It did not finish. It was stopped, which is a different fact."""
        make_contacts(3, connection=connection)
        broadcast = make_broadcast(connection=connection)
        services.schedule_broadcast(broadcast)

        services.cancel_broadcast(broadcast)

        assert not _deliveries(tenancy.workspace).exists()

    def test_the_delivery_row_names_no_contact(
        self, tenancy, webhook, make_contacts, make_broadcast, connection, adapter_for
    ):
        """A contact-bearing queue row runs under that contact's advisory lock.

        A broadcast-wide event belongs to no contact, and holding one contact's
        lock across a call to somebody else's server would stall their messages
        behind a slow receiver (SPEC §9.6).
        """
        make_contacts(1, connection=connection)
        broadcast = make_broadcast(connection=connection)

        with adapter_for(connection.platform):
            _run_to_completion(tenancy.workspace, broadcast)

        assert _deliveries(tenancy.workspace).get().contact_id is None


@pytest.mark.django_db
class TestNotification:
    def test_the_operator_who_sent_it_gets_a_bell_notification(
        self, tenancy, make_contacts, make_broadcast, connection, adapter_for
    ):
        from apps.notifications.models import Notification

        make_contacts(2, connection=connection)
        broadcast = make_broadcast(connection=connection)

        with adapter_for(connection.platform):
            _run_to_completion(tenancy.workspace, broadcast)

        notification = Notification.objects.get(user=tenancy.owner, event_type="broadcast_finished")
        assert broadcast.name in notification.title

    def test_a_notification_failure_does_not_undo_the_settle(
        self, tenancy, make_contacts, make_broadcast, connection, adapter_for, monkeypatch
    ):
        """The broadcast *did* finish. A bell that will not ring is not a reason
        to make the worker retry ten thousand recipients' worth of bookkeeping."""
        import apps.notifications.engine as engine

        def explode(*args, **kwargs):
            raise RuntimeError("no bell today")

        monkeypatch.setattr(engine, "notify", explode)
        make_contacts(1, connection=connection)
        broadcast = make_broadcast(connection=connection)

        with adapter_for(connection.platform):
            _run_to_completion(tenancy.workspace, broadcast)

        assert broadcast.status == BroadcastStatus.SENT


@pytest.mark.django_db
class TestSettle:
    def test_it_finishes_when_the_last_recipient_settles(
        self, tenancy, make_contacts, make_broadcast, connection, adapter_for
    ):
        make_contacts(3, connection=connection)
        broadcast = make_broadcast(connection=connection)

        with adapter_for(connection.platform):
            services.schedule_broadcast(broadcast)
            fanout = (
                ScheduledAction.objects.for_workspace(tenancy.workspace).filter(type=ActionType.BROADCAST_FANOUT).get()
            )
            handlers.handle_broadcast_fanout(fanout.payload, fanout)
            # Marked done the way the worker would: a fanout row left ``pending``
            # is work still owed, and settle() refuses while any is outstanding.
            ScheduledAction.objects.for_workspace(tenancy.workspace).filter(pk=fanout.pk).update(
                status=ActionStatus.DONE
            )
            sends = list(
                ScheduledAction.objects.for_workspace(tenancy.workspace).filter(type=ActionType.BROADCAST_SEND)
            )
            for action in sends[:2]:
                handlers.handle_broadcast_send(action.payload, action)

            broadcast.refresh_from_db()
            assert broadcast.status == BroadcastStatus.SENDING

            handlers.handle_broadcast_send(sends[2].payload, sends[2])

        broadcast.refresh_from_db()
        assert broadcast.status == BroadcastStatus.SENT
        assert broadcast.finished_at is not None
        assert broadcast.stats["sent"] == 3

    def test_the_housekeeping_sweep_rescues_a_broadcast_whose_sends_gave_up(
        self, tenancy, make_contacts, make_broadcast, connection
    ):
        """The hole the sweep exists to close, made concrete.

        A handler that raises rolls its transaction back; after ``max_attempts``
        the queue marks the action ``failed`` and stops. The recipient row is then
        ``pending`` with nothing left to move it, so the broadcast would sit at
        ``sending`` forever and never announce itself finished.
        """
        from apps.broadcasts.housekeeping import settle_broadcasts
        from apps.broadcasts.models import RecipientStatus
        from apps.messaging.codes import Failure

        make_contacts(2, connection=connection)
        broadcast = make_broadcast(connection=connection)
        services.schedule_broadcast(broadcast)
        fanout = ScheduledAction.objects.for_workspace(tenancy.workspace).filter(type=ActionType.BROADCAST_FANOUT).get()
        handlers.handle_broadcast_fanout(fanout.payload, fanout)
        # Marked done the way the worker would: a fanout row left ``pending``
        # is work still owed, and settle() refuses while any is outstanding.
        ScheduledAction.objects.for_workspace(tenancy.workspace).filter(pk=fanout.pk).update(status=ActionStatus.DONE)
        ScheduledAction.objects.for_workspace(tenancy.workspace).filter(type=ActionType.BROADCAST_SEND).update(
            status=ActionStatus.FAILED
        )

        summary = settle_broadcasts()

        broadcast.refresh_from_db()
        assert broadcast.status == BroadcastStatus.SENT
        assert (
            broadcast.recipients.filter(status=RecipientStatus.FAILED, reason=Failure.RETRIES_EXHAUSTED.value).count()
            == 2
        )
        assert summary and "2 stranded" in summary

    def test_the_sweep_leaves_a_broadcast_with_live_work_alone(
        self, tenancy, make_contacts, make_broadcast, connection
    ):
        from apps.broadcasts.housekeeping import settle_broadcasts

        make_contacts(2, connection=connection)
        broadcast = make_broadcast(connection=connection)
        services.schedule_broadcast(broadcast)
        fanout = ScheduledAction.objects.for_workspace(tenancy.workspace).filter(type=ActionType.BROADCAST_FANOUT).get()
        handlers.handle_broadcast_fanout(fanout.payload, fanout)
        # Marked done the way the worker would: a fanout row left ``pending``
        # is work still owed, and settle() refuses while any is outstanding.
        ScheduledAction.objects.for_workspace(tenancy.workspace).filter(pk=fanout.pk).update(status=ActionStatus.DONE)

        settle_broadcasts()

        broadcast.refresh_from_db()
        assert broadcast.status == BroadcastStatus.SENDING


@pytest.mark.django_db
class TestEmptyFanout:
    def test_an_audience_that_vanished_between_scheduling_and_fanout_still_finishes(
        self, tenancy, make_contacts, make_broadcast, connection
    ):
        """The narrow case that used to leave a broadcast stuck at ``sending``.

        Scheduling refuses an audience nobody matches, but the audience is
        resolved *again* at fanout — and a workspace can delete every one of
        those contacts in between. With nothing queued and nothing pending, a
        counter definition that required ``queued > 0`` never called it finished,
        and the housekeeping sweep asked the same question and agreed.
        """
        from apps.contacts.models import Contact, ContactStatus

        contacts = make_contacts(3, connection=connection)
        broadcast = make_broadcast(connection=connection)
        services.schedule_broadcast(broadcast)

        Contact.objects.for_workspace(tenancy.workspace).filter(pk__in=[contact.pk for contact in contacts]).update(
            status=ContactStatus.DELETED
        )

        fanout = ScheduledAction.objects.for_workspace(tenancy.workspace).filter(type=ActionType.BROADCAST_FANOUT).get()
        handlers.handle_broadcast_fanout(fanout.payload, fanout)
        # Marked done the way the worker would: a fanout row left ``pending``
        # is work still owed, and settle() refuses while any is outstanding.
        ScheduledAction.objects.for_workspace(tenancy.workspace).filter(pk=fanout.pk).update(status=ActionStatus.DONE)

        broadcast.refresh_from_db()
        assert broadcast.status == BroadcastStatus.SENT
        assert broadcast.recipients.count() == 0
