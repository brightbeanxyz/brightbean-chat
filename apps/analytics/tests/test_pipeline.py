"""Counters against a real send through the messaging facade (SPEC §18).

    Per-node counters (sent, delivered, failed, clicked) via ``node_stat_daily``
    upserts from the send pipeline […]

Nothing here fakes the facade. The claim being tested is that a counter agrees
with what the product actually did, and a fake ``send_outbound`` would let it be
right about a send that never happened.

The hook sites are the three terminal-status writes in
``apps/messaging/services.py`` and the receipt ladder in
``apps/messaging/ingest.py``; there is no second path, which is what
``apps/flows/engine/sending.py`` warns against and what makes a message routed
around the facade invisible here by construction rather than by accident.
"""

from typing import Any

import pytest
from django.utils import timezone

from apps.analytics.models import NodeStatDaily
from apps.analytics.tests.conftest import ENTRY_NODE, TEXT, make_execution
from apps.channels.events import EventPayload, EventType, NormalizedEvent, SendResult, SendStatus
from apps.channels.providers.exceptions import APIError
from apps.channels.tests.fake_adapter import registered
from apps.common.platforms import Platform
from apps.flows.messaging import message_idempotency_key
from apps.messaging import services
from apps.messaging.ingest import persist_events
from apps.messaging.models import Message, MessageStatus

pytestmark = pytest.mark.django_db


def counters_for(workspace: Any, flow: Any, node_id: str = ENTRY_NODE) -> dict[str, int]:
    row = (
        NodeStatDaily.objects.for_workspace(workspace)
        .filter(flow=flow, node_id=node_id, date=timezone.now().date())
        .first()
    )
    if row is None:
        return {"sent": 0, "delivered": 0, "failed": 0, "clicked": 0}
    return {"sent": row.sent, "delivered": row.delivered, "failed": row.failed, "clicked": row.clicked}


def send(tenancy: Any, contact: Any, connection: Any, execution: Any, *, attempt: int = 0) -> Message:
    return services.send_outbound(
        workspace=tenancy.workspace,
        contact=contact,
        connection=connection,
        outbound=TEXT,
        source="automation",
        idempotency_key=message_idempotency_key(execution, ENTRY_NODE, attempt),
    )


def receipt(connection: Any, provider_message_id: str, status: str) -> None:
    """One ``delivery_status`` event, through the real persistence processor."""
    persist_events(
        connection,
        [
            NormalizedEvent(
                type=EventType.DELIVERY_STATUS,
                connection=connection,
                platform_user_id="u1",
                provider_event_id=f"receipt-{provider_message_id}-{status}",
                timestamp=timezone.now(),
                payload=EventPayload(extra={"provider_message_id": provider_message_id, "status": status}),
            )
        ],
    )


class TestSentAndFailed:
    def test_a_successful_send_counts_one(
        self, tenancy: Any, contact: Any, connection: Any, identity: Any, flow: Any
    ) -> None:
        execution = make_execution(flow, contact, connection)

        with registered(Platform.TELEGRAM):
            message = send(tenancy, contact, connection, execution)

        assert message.status == MessageStatus.SENT
        assert counters_for(tenancy.workspace, flow) == {"sent": 1, "delivered": 0, "failed": 0, "clicked": 0}

    def test_a_compliance_denial_counts_a_failure(self, tenancy: Any, contact: Any, connection: Any, flow: Any) -> None:
        """No identity, so ``can_send`` refuses. Contract 1 returns a failed row
        rather than raising, and a refused send is a failed one for the flow
        author looking at the node."""
        execution = make_execution(flow, contact, connection)

        with registered(Platform.TELEGRAM):
            message = send(tenancy, contact, connection, execution)

        assert message.status == MessageStatus.FAILED
        assert counters_for(tenancy.workspace, flow)["failed"] == 1

    def test_a_permanent_provider_error_counts_a_failure(
        self, tenancy: Any, contact: Any, connection: Any, identity: Any, flow: Any
    ) -> None:
        execution = make_execution(flow, contact, connection)

        with registered(Platform.TELEGRAM) as adapter:

            def refuse(self: Any, conn: Any, ident: Any, outbound: Any) -> SendResult:
                raise APIError("rejected", status_code=400)

            adapter.send = refuse  # type: ignore[method-assign,assignment]
            message = send(tenancy, contact, connection, execution)

        assert message.status == MessageStatus.FAILED
        assert counters_for(tenancy.workspace, flow) == {"sent": 0, "delivered": 0, "failed": 1, "clicked": 0}

    def test_the_same_key_sent_twice_counts_once(
        self, tenancy: Any, contact: Any, connection: Any, identity: Any, flow: Any
    ) -> None:
        """SPEC §9.4's idempotency, seen from the counter: a retried step that
        finds its own row does not call the provider again and must not move the
        number again either."""
        execution = make_execution(flow, contact, connection)

        with registered(Platform.TELEGRAM):
            send(tenancy, contact, connection, execution)
            send(tenancy, contact, connection, execution)

        assert counters_for(tenancy.workspace, flow)["sent"] == 1

    def test_a_withdrawn_send_counts_a_failure(
        self, tenancy: Any, contact: Any, connection: Any, identity: Any, flow: Any
    ) -> None:
        """Contract 1's ``withdraw_send``: a message accepted but never
        dispatched. SPEC §5 has one word for that outcome and it is ``failed``."""
        execution = make_execution(flow, contact, connection)
        message = Message.objects.create(
            conversation=services.open_conversation(
                workspace=tenancy.workspace, contact=contact, connection=connection
            ),
            direction="out",
            source="broadcast",
            status=MessageStatus.QUEUED,
            idempotency_key=message_idempotency_key(execution, ENTRY_NODE),
            body=TEXT.to_body(),
        )

        services.withdraw_send(message, reason="broadcast_cancelled")

        assert counters_for(tenancy.workspace, flow)["failed"] == 1

    def test_a_preview_run_moves_nothing(
        self, tenancy: Any, contact: Any, connection: Any, identity: Any, flow: Any
    ) -> None:
        execution = make_execution(flow, contact, connection, preview=True)

        with registered(Platform.TELEGRAM):
            message = send(tenancy, contact, connection, execution)

        assert message.status == MessageStatus.SENT
        assert not NodeStatDaily.objects.for_workspace(tenancy.workspace).exists()

    def test_a_send_with_no_flow_behind_it_moves_nothing(
        self, tenancy: Any, contact: Any, connection: Any, identity: Any
    ) -> None:
        """An agent reply from the inbox. It has no node, so it has no counter —
        and it must not blow up on the way past one."""
        with registered(Platform.TELEGRAM):
            message = services.send_outbound(
                workspace=tenancy.workspace,
                contact=contact,
                connection=connection,
                outbound=TEXT,
                source="agent",
                idempotency_key="agent:1",
            )

        assert message.status == MessageStatus.SENT
        assert not NodeStatDaily.objects.unscoped().exists()


class TestDelivered:
    def test_a_delivery_receipt_counts_a_delivery(
        self, tenancy: Any, contact: Any, connection: Any, identity: Any, flow: Any
    ) -> None:
        execution = make_execution(flow, contact, connection)

        with registered(Platform.TELEGRAM) as adapter:

            def accept(self: Any, conn: Any, ident: Any, outbound: Any) -> SendResult:
                return SendResult(status=SendStatus.SENT, provider_message_id="pm-1")

            adapter.send = accept  # type: ignore[method-assign,assignment]
            send(tenancy, contact, connection, execution)

        receipt(connection, "pm-1", MessageStatus.DELIVERED)

        assert counters_for(tenancy.workspace, flow) == {"sent": 1, "delivered": 1, "failed": 0, "clicked": 0}

    def test_a_read_receipt_after_a_delivery_does_not_count_twice(
        self, tenancy: Any, contact: Any, connection: Any, identity: Any, flow: Any
    ) -> None:
        execution = make_execution(flow, contact, connection)

        with registered(Platform.TELEGRAM) as adapter:

            def accept(self: Any, conn: Any, ident: Any, outbound: Any) -> SendResult:
                return SendResult(status=SendStatus.SENT, provider_message_id="pm-2")

            adapter.send = accept  # type: ignore[method-assign,assignment]
            send(tenancy, contact, connection, execution)

        receipt(connection, "pm-2", MessageStatus.DELIVERED)
        receipt(connection, "pm-2", MessageStatus.READ)

        assert counters_for(tenancy.workspace, flow)["delivered"] == 1

    def test_a_late_sent_receipt_walks_nothing_backwards(
        self, tenancy: Any, contact: Any, connection: Any, identity: Any, flow: Any
    ) -> None:
        """Platforms do not promise receipt ordering. The ladder refuses the
        move, so there is nothing for the counter to double."""
        execution = make_execution(flow, contact, connection)

        with registered(Platform.TELEGRAM) as adapter:

            def accept(self: Any, conn: Any, ident: Any, outbound: Any) -> SendResult:
                return SendResult(status=SendStatus.SENT, provider_message_id="pm-3")

            adapter.send = accept  # type: ignore[method-assign,assignment]
            send(tenancy, contact, connection, execution)

        receipt(connection, "pm-3", MessageStatus.DELIVERED)
        receipt(connection, "pm-3", MessageStatus.SENT)

        assert counters_for(tenancy.workspace, flow) == {"sent": 1, "delivered": 1, "failed": 0, "clicked": 0}
