"""Cancellation, against a partially-drained queue.

The issue puts this in capitals: *"a broadcast cancelled mid-fanout stops
scheduling AND skips already-scheduled sends that have not run. Test it against a
partially-drained queue."*

Three mechanisms have to hold together, and each is a separate test below because
each fails differently:

1. the **status**, under a row lock, which is what a claimed action re-reads;
2. the **pending queue rows**, bulk-flipped to ``cancelled``;
3. the **pending recipient rows**, so the counters reconcile at once rather than
   after a sweep.

The one that is easy to get wrong is (1). A row a worker has already claimed is
``running`` and the bulk flip cannot reach it, so if the handler did not refuse
itself, cancelling a broadcast mid-drain would still deliver the batch in flight.
"""

import pytest

from apps.broadcasts import handlers, services
from apps.broadcasts.models import BroadcastStatus, RecipientStatus
from apps.queueing.models import ActionStatus, ActionType, ScheduledAction


def _actions(workspace, action_type):
    return ScheduledAction.objects.for_workspace(workspace).filter(type=action_type)


def _fan_out(workspace, broadcast):
    services.schedule_broadcast(broadcast)
    action = _actions(workspace, ActionType.BROADCAST_FANOUT).get()
    handlers.handle_broadcast_fanout(action.payload, action)
    _actions(workspace, ActionType.BROADCAST_FANOUT).filter(pk=action.pk).update(status=ActionStatus.DONE)
    broadcast.refresh_from_db()
    return list(_actions(workspace, ActionType.BROADCAST_SEND).order_by("run_at"))


@pytest.mark.django_db
class TestPartiallyDrainedQueue:
    def test_cancelling_mid_drain_leaves_no_send_behind(
        self, tenancy, make_contacts, make_broadcast, connection, adapter_for
    ):
        """The acceptance criterion, end to end.

        Ten recipients, four delivered, then cancel, then drain the rest: the six
        that had not run must not run. The already-sent four stand — there is no
        unsend.
        """
        make_contacts(10, connection=connection)
        broadcast = make_broadcast(connection=connection)

        with adapter_for(connection.platform) as adapter:
            actions = _fan_out(tenancy.workspace, broadcast)
            for action in actions[:4]:
                handlers.handle_broadcast_send(action.payload, action)
                _actions(tenancy.workspace, ActionType.BROADCAST_SEND).filter(pk=action.pk).update(
                    status=ActionStatus.DONE
                )
            assert len(adapter.sends) == 4

            services.cancel_broadcast(broadcast)

            # Drain what is left, exactly as a worker would: the rows it claims
            # are the ones still pending, and the handler is called for each.
            for action in actions[4:]:
                action.refresh_from_db()
                handlers.handle_broadcast_send(action.payload, action)

            assert len(adapter.sends) == 4, "a cancelled broadcast delivered more messages"

        broadcast.refresh_from_db()
        assert broadcast.status == BroadcastStatus.CANCELLED
        counts = services.counters(broadcast)
        assert counts.sent == 4
        assert counts.cancelled == 6
        assert counts.pending == 0

    def test_the_counters_reconcile_after_a_cancellation(
        self, tenancy, make_contacts, make_broadcast, connection, adapter_for
    ):
        """queued = sent + failed + cancelled + skipped, with a skip in the mix."""
        make_contacts(6, connection=connection)
        make_contacts(2, connection=connection, opted_out=True, prefix="out")
        broadcast = make_broadcast(connection=connection)

        with adapter_for(connection.platform):
            actions = _fan_out(tenancy.workspace, broadcast)
            for action in actions[:2]:
                handlers.handle_broadcast_send(action.payload, action)
            services.cancel_broadcast(broadcast)
            for action in actions[2:]:
                action.refresh_from_db()
                handlers.handle_broadcast_send(action.payload, action)

        counts = services.counters(broadcast)
        assert counts.queued == 8
        assert counts.sent == 2
        assert counts.skipped == 2
        assert counts.cancelled == 4
        assert counts.queued == counts.sent + counts.failed + counts.cancelled + counts.skipped

    def test_pending_queue_rows_are_flipped_to_cancelled(
        self, tenancy, make_contacts, make_broadcast, connection, adapter_for
    ):
        """Mechanism 2. Without it the worker would run six no-op handlers."""
        make_contacts(6, connection=connection)
        broadcast = make_broadcast(connection=connection)

        with adapter_for(connection.platform):
            actions = _fan_out(tenancy.workspace, broadcast)
            for action in actions[:2]:
                handlers.handle_broadcast_send(action.payload, action)
                _actions(tenancy.workspace, ActionType.BROADCAST_SEND).filter(pk=action.pk).update(
                    status=ActionStatus.DONE
                )

            services.cancel_broadcast(broadcast)

        remaining = _actions(tenancy.workspace, ActionType.BROADCAST_SEND)
        assert remaining.filter(status=ActionStatus.PENDING).count() == 0
        assert remaining.filter(status=ActionStatus.CANCELLED).count() == 4

    def test_a_send_already_claimed_refuses_itself(
        self, tenancy, make_contacts, make_broadcast, connection, adapter_for
    ):
        """Mechanism 1, isolated — the half the bulk flip cannot do.

        A row a worker claimed is ``running`` when the cancel lands, so the flip
        misses it and the handler is called anyway. It re-reads the broadcast's
        status and declines, which is the only thing that can stop it.
        """
        make_contacts(2, connection=connection)
        broadcast = make_broadcast(connection=connection)

        with adapter_for(connection.platform) as adapter:
            actions = _fan_out(tenancy.workspace, broadcast)
            in_flight = actions[0]
            _actions(tenancy.workspace, ActionType.BROADCAST_SEND).filter(pk=in_flight.pk).update(
                status=ActionStatus.RUNNING
            )

            services.cancel_broadcast(broadcast)
            in_flight.refresh_from_db()
            assert in_flight.status == ActionStatus.RUNNING, "the flip should not reach a claimed row"

            handlers.handle_broadcast_send(in_flight.payload, in_flight)

            assert adapter.sends == []

        recipient = broadcast.recipients.get(pk=in_flight.payload["recipient_id"])
        assert recipient.status == RecipientStatus.CANCELLED

    def test_cancelling_mid_fanout_stops_the_audience_expanding(
        self, tenancy, make_contacts, make_broadcast, connection, monkeypatch
    ):
        """Mechanism 1 again, on the other handler. Not one row more is written."""
        monkeypatch.setattr(handlers, "CHUNK_SIZE", 4)
        make_contacts(20, connection=connection)
        broadcast = make_broadcast(connection=connection)
        services.schedule_broadcast(broadcast)

        first = _actions(tenancy.workspace, ActionType.BROADCAST_FANOUT).get()
        handlers.handle_broadcast_fanout(first.payload, first)
        _actions(tenancy.workspace, ActionType.BROADCAST_FANOUT).filter(pk=first.pk).update(status=ActionStatus.DONE)
        successor = _actions(tenancy.workspace, ActionType.BROADCAST_FANOUT).filter(status=ActionStatus.PENDING).get()

        services.cancel_broadcast(broadcast)

        # As if the successor had been claimed a millisecond before the cancel.
        successor.refresh_from_db()
        handlers.handle_broadcast_fanout(successor.payload, successor)

        assert broadcast.recipients.count() == 4


@pytest.mark.django_db
class TestRefusals:
    def test_a_draft_cannot_be_cancelled(self, make_contacts, make_broadcast, connection):
        """There is nothing in the queue to stop; delete it instead."""
        make_contacts(1, connection=connection)
        broadcast = make_broadcast(connection=connection)

        with pytest.raises(services.BroadcastError, match="scheduled or sending"):
            services.cancel_broadcast(broadcast)

    def test_cancelling_twice_is_refused_rather_than_silently_repeated(
        self, tenancy, make_contacts, make_broadcast, connection
    ):
        make_contacts(1, connection=connection)
        broadcast = make_broadcast(connection=connection)
        services.schedule_broadcast(broadcast)
        services.cancel_broadcast(broadcast)

        with pytest.raises(services.BroadcastError):
            services.cancel_broadcast(broadcast)

    def test_a_cancelled_broadcast_can_be_deleted(self, make_contacts, make_broadcast, connection):
        make_contacts(1, connection=connection)
        broadcast = make_broadcast(connection=connection)
        services.schedule_broadcast(broadcast)
        services.cancel_broadcast(broadcast)

        services.delete_broadcast(broadcast)

    def test_a_live_broadcast_cannot_be_deleted(self, make_contacts, make_broadcast, connection):
        """Deleting one mid-send would orphan the rows about to look for it."""
        make_contacts(1, connection=connection)
        broadcast = make_broadcast(connection=connection)
        services.schedule_broadcast(broadcast)

        with pytest.raises(services.BroadcastError, match="Cancel this broadcast"):
            services.delete_broadcast(broadcast)


@pytest.mark.django_db
def test_cancelling_retires_the_private_mini_flow(tenancy, make_contacts, make_broadcast, connection):
    """Nothing will run it again, so it leaves the flow list.

    Archived only now, and not earlier, because ``start_flow`` refuses an
    archived flow — the copy has to stay runnable while the queue might reach it.
    """
    from apps.flows.models import FlowStatus

    make_contacts(1, connection=connection)
    broadcast = make_broadcast(connection=connection)
    services.schedule_broadcast(broadcast)
    broadcast.refresh_from_db()
    assert broadcast.flow.status == FlowStatus.ACTIVE

    services.cancel_broadcast(broadcast)

    broadcast.refresh_from_db()
    assert broadcast.flow.status == FlowStatus.ARCHIVED


@pytest.mark.django_db
def test_cancelling_stops_a_send_the_token_bucket_had_deferred(
    tenancy, make_contacts, make_broadcast, connection, adapter_for, settings
):
    """The narrowest window in cancellation, and it is a real one.

    A ``broadcast_send`` that runs while the connection's bucket is empty does
    not fail: SPEC §8 has the facade queue the message and arm a ``send_retry``.
    That row is not a ``broadcast_send``, so the bulk flip does not reach it —
    and without cancelling it too, a broadcast stopped at that moment would still
    deliver minutes later, when the retry fired.
    """
    from datetime import timedelta

    from django.utils import timezone as tz

    from apps.messaging.buckets import rate_for
    from apps.messaging.models import MessageStatus, SendBucket

    settings.SEND_BUCKET_MAX_WAIT_SECONDS = 0
    # Two recipients, one send run: the second keeps the broadcast at ``sending``,
    # which is the state a cancel is for. With only one, the deferred send would
    # settle it — a queued message is recorded as on its way — and there would be
    # nothing left to cancel.
    make_contacts(2, connection=connection)
    broadcast = make_broadcast(connection=connection)

    with adapter_for(connection.platform) as adapter:
        actions = _fan_out(tenancy.workspace, broadcast)
        SendBucket.objects.create(
            connection=connection,
            tokens=0.0,
            capacity=1.0,
            refill_rate=rate_for(connection.platform),
            refilled_at=tz.now() + timedelta(hours=1),
        )
        handlers.handle_broadcast_send(actions[0].payload, actions[0])

        assert adapter.sends == []

    recipient = broadcast.recipients.get(pk=actions[0].payload["recipient_id"])
    assert recipient.status == RecipientStatus.SENT
    assert recipient.message.status == MessageStatus.QUEUED
    retry = ScheduledAction.objects.for_workspace(tenancy.workspace).filter(type=ActionType.SEND_RETRY)
    assert retry.filter(status=ActionStatus.PENDING).exists()

    services.cancel_broadcast(broadcast)

    assert not retry.filter(status=ActionStatus.PENDING).exists()
    recipient.refresh_from_db()
    assert recipient.status == RecipientStatus.CANCELLED


@pytest.mark.django_db
def test_a_cancel_does_not_rewrite_a_recipient_that_was_already_delivered(
    tenancy, make_contacts, make_broadcast, connection, adapter_for
):
    """ "Already-sent stand" has to survive a re-run, not only the bulk flip.

    A send action that was ``running`` when the cancel landed is out of the bulk
    flip's reach, and zombie recovery returns it to ``pending`` for a second run.
    If the handler checked the broadcast's cancellation before it checked the
    recipient's own status, that second run would rewrite a recipient who had
    already received the message to ``cancelled`` — losing the record of a
    message on somebody's phone, and moving a count from ``sent`` to
    ``cancelled`` for a send that demonstrably happened.
    """
    make_contacts(2, connection=connection)
    broadcast = make_broadcast(connection=connection)

    with adapter_for(connection.platform) as adapter:
        actions = _fan_out(tenancy.workspace, broadcast)
        delivered = actions[0]
        handlers.handle_broadcast_send(delivered.payload, delivered)
        assert len(adapter.sends) == 1

        services.cancel_broadcast(broadcast)

        # The re-run zombie recovery would produce, against a cancelled broadcast.
        handlers.handle_broadcast_send(delivered.payload, delivered)

        assert len(adapter.sends) == 1, "it must not send again either"

    recipient = broadcast.recipients.get(pk=delivered.payload["recipient_id"])
    assert recipient.status == RecipientStatus.SENT
    assert recipient.message_id is not None

    counts = services.counters(broadcast)
    assert counts.sent == 1
    assert counts.cancelled == 1
    assert counts.queued == counts.sent + counts.failed + counts.cancelled + counts.skipped


@pytest.mark.django_db
def test_a_retry_already_running_is_not_recorded_as_cancelled(
    tenancy, make_contacts, make_broadcast, connection, adapter_for, settings
):
    """Only the retries this cancel actually stopped may settle their recipients.

    ``handle_send_retry`` knows nothing about broadcasts, so a retry that was
    already claimed will finish its provider call. Recording its recipient as
    cancelled would report a message on its way to somebody's phone as stopped,
    and take a count out of ``sent`` for a send that happened.
    """
    from datetime import timedelta

    from django.utils import timezone as tz

    from apps.messaging.buckets import rate_for
    from apps.messaging.models import SendBucket
    from apps.queueing.models import ActionType as QueueType

    settings.SEND_BUCKET_MAX_WAIT_SECONDS = 0
    # Three recipients, two sends run: the third keeps the broadcast at
    # ``sending``, which is the state a cancel is for.
    make_contacts(3, connection=connection)
    broadcast = make_broadcast(connection=connection)

    with adapter_for(connection.platform):
        actions = _fan_out(tenancy.workspace, broadcast)
        SendBucket.objects.create(
            connection=connection,
            tokens=0.0,
            capacity=1.0,
            refill_rate=rate_for(connection.platform),
            refilled_at=tz.now() + timedelta(hours=1),
        )
        for action in actions[:2]:
            handlers.handle_broadcast_send(action.payload, action)

    retries = ScheduledAction.objects.for_workspace(tenancy.workspace).filter(type=QueueType.SEND_RETRY)
    assert retries.count() == 2
    # One of them is already claimed when the cancel lands.
    claimed = retries.first()
    retries.filter(pk=claimed.pk).update(status=ActionStatus.RUNNING)

    services.cancel_broadcast(broadcast)

    claimed.refresh_from_db()
    assert claimed.status == ActionStatus.RUNNING, "the flip must not reach a claimed row"

    counts = services.counters(broadcast)
    # One deferred send stopped, one left alone because its retry was already
    # claimed, and one recipient whose send never ran at all.
    assert counts.cancelled == 2, "the stopped retry, plus the recipient that never ran"
    assert counts.sent == 1, "the in-flight one is still on its way, and is reported as such"
    assert counts.queued == counts.sent + counts.failed + counts.cancelled + counts.skipped
