"""The upsert (SPEC §5) and the transition arithmetic behind it.

The acceptance criterion this file exists for is "parallel increments never lose
counts", and it is asserted the only way that claim can be: with real threads, on
real connections, against a real Postgres.
"""

import threading
from datetime import date, timedelta
from typing import Any

import pytest
from django.db import connections
from django.utils import timezone

from apps.analytics.counters import bump, deltas_for, record_click, record_message_status
from apps.analytics.models import NodeStatDaily
from apps.flows.fixtures import graph_for
from apps.flows.tests.support import published_flow
from apps.messaging.models import MessageStatus
from tests.support import create_tenancy

pytestmark = pytest.mark.django_db

#: Enough concurrency for a lost update to show up, few enough that the barrier
#: does not outrun the test database's connection limit.
THREADS = 20


def row_for(workspace: Any, flow: Any, node_id: str = "n1", day: date | None = None) -> NodeStatDaily:
    return NodeStatDaily.objects.for_workspace(workspace).get(
        flow=flow, node_id=node_id, date=day or timezone.now().date()
    )


class TestBump:
    def test_the_first_call_inserts_and_the_second_adds(self, tenancy: Any, flow: Any) -> None:
        bump(workspace_id=tenancy.workspace.pk, flow_id=flow.pk, node_id="n1", sent=1)
        bump(workspace_id=tenancy.workspace.pk, flow_id=flow.pk, node_id="n1", sent=1, clicked=2)

        row = row_for(tenancy.workspace, flow)
        assert (row.sent, row.delivered, row.failed, row.clicked) == (2, 0, 0, 2)
        assert NodeStatDaily.objects.for_workspace(tenancy.workspace).count() == 1

    def test_each_day_is_its_own_row(self, tenancy: Any, flow: Any) -> None:
        yesterday = timezone.now().date() - timedelta(days=1)
        bump(workspace_id=tenancy.workspace.pk, flow_id=flow.pk, node_id="n1", sent=1)
        bump(workspace_id=tenancy.workspace.pk, flow_id=flow.pk, node_id="n1", day=yesterday, sent=4)

        assert row_for(tenancy.workspace, flow).sent == 1
        assert row_for(tenancy.workspace, flow, day=yesterday).sent == 4

    def test_each_node_is_its_own_row(self, tenancy: Any, flow: Any) -> None:
        bump(workspace_id=tenancy.workspace.pk, flow_id=flow.pk, node_id="n1", sent=1)
        bump(workspace_id=tenancy.workspace.pk, flow_id=flow.pk, node_id="n2", sent=1)

        assert NodeStatDaily.objects.for_workspace(tenancy.workspace).count() == 2

    def test_all_zero_deltas_write_nothing(self, tenancy: Any, flow: Any) -> None:
        bump(workspace_id=tenancy.workspace.pk, flow_id=flow.pk, node_id="n1")

        assert not NodeStatDaily.objects.for_workspace(tenancy.workspace).exists()

    def test_an_unknown_counter_is_a_programming_error(self, tenancy: Any, flow: Any) -> None:
        """Silently ignoring it would be a counter that never moves and never
        says why."""
        with pytest.raises(ValueError, match="opened"):
            bump(workspace_id=tenancy.workspace.pk, flow_id=flow.pk, node_id="n1", opened=1)

    def test_record_click_adds_exactly_one(self, tenancy: Any, flow: Any) -> None:
        record_click(workspace_id=tenancy.workspace.pk, flow_id=flow.pk, node_id="n1")

        assert row_for(tenancy.workspace, flow).clicked == 1


@pytest.mark.django_db(transaction=True)
class TestConcurrency:
    """SPEC §18's acceptance criterion: parallel increments never lose counts.

    Nothing is mocked. Real threads, real connections, the real statement — what
    makes the total right is ``ON CONFLICT … DO UPDATE SET sent = <table>.sent +
    EXCLUDED.sent`` serialising the conflicting inserters on the row itself, so
    "no count is lost" is a property of one statement rather than of the threads
    agreeing to take turns.

    ``transaction=True`` because that is what gives each thread a connection that
    can commit; the ordinary ``django_db`` fixture wraps everything in one
    transaction no other thread can see into.
    """

    def test_twenty_threads_each_adding_one_end_at_twenty(self) -> None:
        tenancy = create_tenancy("counters")
        flow = published_flow(tenancy.workspace, graph_for("send_message"))
        workspace_id, flow_id = tenancy.workspace.pk, flow.pk
        start = threading.Barrier(THREADS)
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                # Every thread waits for the last one, so the increments land in
                # the same instant rather than politely one after another.
                start.wait(timeout=30)
                bump(workspace_id=workspace_id, flow_id=flow_id, node_id="n1", clicked=1)
            except BaseException as exc:  # noqa: BLE001 - re-raised in the assertion below
                errors.append(exc)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=worker, name=f"bump-{index}") for index in range(THREADS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
            assert not thread.is_alive(), f"{thread.name} did not finish"

        assert not errors, errors
        assert row_for(tenancy.workspace, flow).clicked == THREADS
        assert NodeStatDaily.objects.for_workspace(tenancy.workspace).count() == 1


class TestDeltas:
    """Counting the rung a message *crossed*, not the one it landed on."""

    def test_queued_to_sent_counts_one_send(self) -> None:
        assert deltas_for(MessageStatus.QUEUED, MessageStatus.SENT) == {"sent": 1}

    def test_sent_to_delivered_counts_a_delivery_and_not_a_second_send(self) -> None:
        assert deltas_for(MessageStatus.SENT, MessageStatus.DELIVERED) == {"delivered": 1}

    def test_delivered_to_read_counts_nothing_new(self) -> None:
        """`read` is past `delivered` on the ladder, and there is no `read`
        counter — SPEC §5 names four columns and this is not one of them."""
        assert deltas_for(MessageStatus.DELIVERED, MessageStatus.READ) == {}

    def test_a_message_that_skips_straight_to_read_counts_both_rungs(self) -> None:
        """Platforms do not promise every rung: Meta routinely sends `read` for a
        message whose `delivered` receipt never arrived."""
        assert deltas_for(MessageStatus.QUEUED, MessageStatus.READ) == {"sent": 1, "delivered": 1}

    def test_writing_the_same_status_twice_counts_once(self) -> None:
        assert deltas_for(MessageStatus.SENT, MessageStatus.SENT) == {}
        assert deltas_for(MessageStatus.FAILED, MessageStatus.FAILED) == {}

    def test_failure_counts_a_failure(self) -> None:
        assert deltas_for(MessageStatus.QUEUED, MessageStatus.FAILED) == {"failed": 1}

    def test_a_late_delivery_receipt_after_a_failure_does_not_re_count_the_send(self) -> None:
        """SPEC §9.5's rule 3: a delivery receipt beats `failed`.

        The arrival is news; the send is not. A message can only reach `failed`
        and *then* collect a delivery receipt if a provider accepted it, so its
        `sent` was counted on the way in — scoring `failed` as rank 0 made this
        step cross that rung a second time and reported one message as two sends.
        """
        assert deltas_for(MessageStatus.FAILED, MessageStatus.DELIVERED) == {"delivered": 1}

    def test_a_whole_failure_and_recovery_counts_one_of_each(self) -> None:
        """The sequence the double count came from, walked end to end."""
        totals = {"sent": 0, "delivered": 0, "failed": 0}
        for previous, current in (
            (MessageStatus.QUEUED, MessageStatus.SENT),
            (MessageStatus.SENT, MessageStatus.FAILED),
            (MessageStatus.FAILED, MessageStatus.DELIVERED),
            (MessageStatus.DELIVERED, MessageStatus.READ),
        ):
            for field, value in deltas_for(previous, current).items():
                totals[field] += value

        assert totals == {"sent": 1, "delivered": 1, "failed": 1}

    def test_a_denial_that_never_reached_a_provider_counts_no_send(self) -> None:
        """The other side of the same asymmetry. `failed` scores as `sent` only
        for the status being *left*: promoting both sides would make an ordinary
        compliance denial credit a send nobody made."""
        assert deltas_for(MessageStatus.QUEUED, MessageStatus.FAILED) == {"failed": 1}

    def test_a_message_off_the_ladder_moves_nothing(self) -> None:
        """`deleted` is not a rung, and SPEC §6.3's redaction is terminal."""
        assert deltas_for(MessageStatus.READ, MessageStatus.DELETED) == {}
        assert deltas_for(MessageStatus.DELETED, MessageStatus.READ) == {}


class TestRecordMessageStatus:
    def test_a_message_with_no_node_behind_it_counts_nothing(self, tenancy: Any, flow: Any) -> None:
        class NotAFlowSend:
            workspace_id = tenancy.workspace.pk
            idempotency_key = "in:some-webhook-event"

        record_message_status(NotAFlowSend(), previous=MessageStatus.QUEUED, current=MessageStatus.SENT)

        assert not NodeStatDaily.objects.for_workspace(tenancy.workspace).exists()

    def test_a_transition_that_moves_nothing_does_not_query(self, tenancy: Any) -> None:
        """`deltas_for` is consulted before attribution, so a no-op receipt costs
        no execution lookup at all."""

        class Exploding:
            workspace_id = tenancy.workspace.pk

            @property
            def idempotency_key(self) -> str:
                raise AssertionError("attribution should not have been attempted")

        record_message_status(Exploding(), previous=MessageStatus.DELIVERED, current=MessageStatus.READ)
