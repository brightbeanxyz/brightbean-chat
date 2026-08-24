"""SPEC §21 phase 3's scenario, starting where the spec starts it.

    Accept when: a Make/Zapier-style scenario (inbound webhook → API contact
    update → flow start) works with only public API + outbound webhooks.

``apps/api/tests/test_acceptance_phase3.py`` owns most of that sentence and owns
it well: it runs the API legs over HTTP, drains the deliveries, and verifies the
signature the way a third-party receiver would — including the negative test
without which the positive one would be vacuous. **Nothing here re-does any of
that**, and in particular the HMAC verification stays that test's.

What it does not do is start from an inbound webhook. Its first step is
``POST /api/v1/contacts``, so the contact is created by the integrator. The
spec's scenario starts one step earlier, with a message from a real person on a
real platform, and that first leg is the one nothing spans: the platform
delivery has to become a contact, a conversation and a message, emit
``message.received``, and reach the integrator's endpoint carrying an id the
integrator can then use — across ``apps.channels``, ``apps.messaging``,
``apps.api`` and ``apps.flows``, none of which can prove it alone.

So this is written as that delta. The chain is:

    signed platform webhook → persistence → message.received on the
    integrator's receiver → (bearer key only) tag the contact → start the flow
    → the reply back on the platform wire → execution.completed

The contact id used in the API calls is read **out of the webhook body**, never
out of the ORM. That is the whole difference between "the pieces work" and "an
integrator who has only our public surface can do this".
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from django.test import Client

from apps.api.delivery import ACTION_TYPE, EVENT_HEADER, SIGNATURE_HEADER, handle_webhook_delivery
from apps.api.models import OutboundWebhook
from apps.api.tests.conftest import bearer, make_key
from apps.api.tests.support import RECEIVER, FakeInternet, serving
from apps.channels.events import TextBlock
from apps.channels.models import ChannelConnection
from apps.channels.tests.fake_adapter import SECRET_HEADER, sign
from apps.channels.tests.fake_adapter import SIGNATURE_HEADER as PLATFORM_SIGNATURE_HEADER
from apps.common.outbound import reset_deployment_cache
from apps.common.platforms import Platform
from apps.flows.models import FlowExecution, Trigger, TriggerType
from apps.flows.tests.routing_support import routing_adapter
from apps.flows.tests.support import graph, node, published_flow
from apps.messaging.models import ContactChannelIdentity, Message, MessageDirection
from apps.queueing.models import ActionStatus, ScheduledAction

pytestmark = pytest.mark.django_db

WEBHOOK_URL = "/webhooks/telegram/"
REPLY = {"blocks": [{"type": "text", "text": "Welcome aboard."}]}


@pytest.fixture(autouse=True)
def _clear_deployment_cache() -> Any:
    reset_deployment_cache()
    yield
    reset_deployment_cache()


@pytest.fixture
def connection(tenancy: Any) -> ChannelConnection:
    row = ChannelConnection(
        workspace=tenancy.workspace,
        platform=Platform.TELEGRAM,
        display_name="Support bot",
        external_id="bot-chain",
    )
    row.rotate_webhook_secret()
    row.save()
    return row


def deliver_platform_event(client: Client, secret: str, *, event_id: str, text: str, user: str = "tg-chain") -> Any:
    """One signed delivery, exactly as a correctly configured platform sends it."""
    body = json.dumps({"events": [{"id": event_id, "user": user, "text": text}]}).encode()
    return client.post(
        WEBHOOK_URL,
        data=body,
        content_type="application/json",
        headers={SECRET_HEADER: secret, PLATFORM_SIGNATURE_HEADER: sign(secret, body)},
    )


def drain_deliveries(workspace: Any) -> None:
    """Run the queued outbound-webhook rows the way the worker would.

    Filtered to this action type on purpose: draining the whole queue here would
    also run whatever else the chain scheduled, and this helper exists to move
    deliveries rather than to be a worker. ``apps/broadcasts/tests/test_fanout.py``
    explains the same choice at more length.
    """
    for action in list(
        ScheduledAction.objects.for_workspace(workspace).filter(type=ACTION_TYPE, status=ActionStatus.PENDING)
    ):
        action.status = ActionStatus.DONE
        action.save(update_fields=["status"])
        handle_webhook_delivery(action.payload, action)


class TestTheChainFromPlatformToIntegrator:
    def test_a_message_from_a_platform_reaches_an_integrator_who_starts_a_flow(
        self, client: Client, tenancy: Any, connection: ChannelConnection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        internet = FakeInternet(serving(200)).install(monkeypatch)

        # --- What the operator configures once, in the UI -------------------
        endpoint = OutboundWebhook(
            workspace=tenancy.workspace,
            url=f"https://{RECEIVER}/hooks",
            events=["message.received", "contact.tag_added", "execution.completed"],
        )
        endpoint.rotate_secret()
        endpoint.save()

        flow = published_flow(tenancy.workspace, graph([node("a", "send_message", REPLY)]), name="Onboarding")
        Trigger(flow=flow, type=TriggerType.API, config_json={"key": "qualified"}).save()

        _, token = make_key(tenancy.workspace, scopes=("read", "write"), name="Make")
        auth = bearer(token)

        with routing_adapter(Platform.TELEGRAM) as adapter:
            # --- Leg 1: a real person messages the bot ----------------------
            # No keyword trigger exists, so nothing here auto-replies: the flow
            # in this scenario is started by the integrator, not by us.
            response = deliver_platform_event(client, connection.webhook_secret, event_id="e1", text="I'm interested")
            assert response.status_code == 200
            assert adapter.sends == [], "nothing should have replied before the integrator asked for it"

            identity = ContactChannelIdentity.objects.for_workspace(tenancy.workspace).get(
                channel_connection=connection
            )
            inbound = Message.objects.for_workspace(tenancy.workspace).filter(direction=MessageDirection.IN)
            assert inbound.count() == 1, "the platform delivery must have become a message"

            # --- Leg 2: the integrator's endpoint hears about it -------------
            drain_deliveries(tenancy.workspace)
            received = [
                json.loads(request.content)
                for request in internet.requests
                if request.headers[EVENT_HEADER] == "message.received"
            ]
            assert len(received) == 1, f"expected one message.received delivery, got {len(internet.requests)}"
            announcement = received[0]
            assert announcement["workspace_id"] == str(tenancy.workspace.pk)
            assert SIGNATURE_HEADER in internet.requests[0].headers, (
                "every delivery is signed; apps/api/tests/test_acceptance_phase3.py owns verifying the bytes"
            )

            # The id the integrator will act on comes out of the delivery, not
            # out of our database. That is the assertion this whole module is for.
            contact_id = announcement["data"]["contact_id"]
            assert contact_id == str(identity.contact_id)

            # --- Leg 3: the integrator acts, with only a bearer token --------
            tagged = client.post(
                f"/api/v1/contacts/{contact_id}/tags",
                data=json.dumps({"name": "qualified"}),
                content_type="application/json",
                **auth,
            )
            assert tagged.status_code == 201

            started = client.post(
                f"/api/v1/contacts/{contact_id}/flows/{flow.pk}/start",
                data=json.dumps({"trigger_key": "qualified", "variables": {"source": "make"}}),
                content_type="application/json",
                **auth,
            )
            assert started.status_code == 202
            execution_id = started.json()["execution_id"]
            assert execution_id, "the flow should have started in the request, not been deferred"

            # --- Leg 4: and the person gets an answer on the platform --------
            texts = [
                block.text for message in adapter.sends for block in message.blocks if isinstance(block, TextBlock)
            ]
            assert texts == ["Welcome aboard."], (
                "the reply has to reach the platform the message came from — the connection was resolved "
                "from the contact's identity, which is the only channel the integrator ever named"
            )

        execution = FlowExecution.objects.for_workspace(tenancy.workspace).get(pk=execution_id)
        assert execution.variables["source"] == "make"

        # --- And the loop closes: the integrator hears it finished ----------
        drain_deliveries(tenancy.workspace)
        events = [request.headers[EVENT_HEADER] for request in internet.requests]
        assert "contact.tag_added" in events
        assert "execution.completed" in events
        assert {request.url.host for request in internet.requests} == {"93.184.216.34"}, (
            "every delivery goes through the SSRF guard and is pinned to the address it checked"
        )


class TestAHostileRedelivery:
    """The same chain, delivered three times, with a malformed sibling event.

    ``apps/channels/tests/test_webhooks.py`` covers hostile payloads and
    deduplication thoroughly *at the endpoint*: injection strings survive as
    data, malformed shapes answer 200 rather than 5xx, a repeated
    ``provider_event_id`` is dropped. What it does not cover is the crossing —
    that the deduplication upstream is what stops a flow running twice and a
    person being messaged twice downstream.

    A platform that gets a 5xx retries the same body until it gives up on the
    webhook entirely, so "answers 200 and does the work once" is one property,
    not two.
    """

    def test_three_deliveries_of_a_hostile_batch_start_one_flow_and_send_one_message(
        self, client: Client, tenancy: Any, connection: ChannelConnection
    ) -> None:
        flow = published_flow(tenancy.workspace, graph([node("a", "send_message", REPLY)]), name="Keyword reply")
        Trigger(
            flow=flow,
            type=TriggerType.KEYWORD,
            config_json={"keywords": [{"text": "start", "mode": "contains"}]},
        ).save()

        # One good event and one the adapter must drop, in the same batch, so the
        # malformed sibling has the chance to take the good one down with it.
        body = json.dumps(
            {
                "events": [
                    {"id": "batch-1", "user": "tg-hostile", "text": "start please"},
                    {"id": "", "user": None, "text": {"nested": "object"}},
                ]
            }
        ).encode()
        headers = {
            SECRET_HEADER: connection.webhook_secret,
            PLATFORM_SIGNATURE_HEADER: sign(connection.webhook_secret, body),
        }

        with routing_adapter(Platform.TELEGRAM) as adapter:
            for attempt in range(3):
                response = client.post(WEBHOOK_URL, data=body, content_type="application/json", headers=headers)
                assert response.status_code == 200, f"delivery {attempt + 1} answered {response.status_code}"

            assert len(adapter.sends) == 1, f"the contact was messaged {len(adapter.sends)} times for one event"

        assert FlowExecution.objects.for_workspace(tenancy.workspace).count() == 1
        assert Message.objects.for_workspace(tenancy.workspace).filter(direction=MessageDirection.OUT).count() == 1
