"""The acceptance tests: a real webhook in, a real Send API call out.

    m.me ref round-trip: referral → ref_url trigger → flow start; get_started →
    welcome flow. Delivery/read receipts update message.status.

Everything between the two ends is the production path — the webhook endpoint,
``X-Hub-Signature-256`` verification against the app secret, deduplication, the
contract-6 seam, L3-A's persistence and compliance and token bucket, L4-A's
ordered routing stages, L3-B's engine, and this adapter. The only substitution is
the network (``httpx.MockTransport``).

Unlike ``test_telegram_e2e``, there is no routing stand-in here: L4-A has merged,
so this runs against the real ``apps.flows.triggers.pipeline``.
"""

import json
from typing import Any

import pytest
from django.test import Client
from django.utils import timezone

from apps.channels.models import ChannelConnection
from apps.channels.tests.messenger_support import fake_graph, load_delivery, post_webhook
from apps.flows.models import LIVE_STATUSES, Flow, FlowExecution, Trigger, TriggerType
from apps.flows.tests.support import edge, graph, node, published_flow
from apps.flows.triggers.services import create_trigger
from apps.messaging.models import Message, MessageDirection, MessageStatus
from tests.support import Tenancy

pytestmark = pytest.mark.django_db


def one_message_flow(workspace: Any, text: str, *, name: str) -> Flow:
    """A published flow whose only step is one message."""
    return published_flow(
        workspace,
        graph(
            [
                node("start", "send_message", {"blocks": [{"type": "text", "text": text}]}),
            ]
        ),
        name=name,
    )


def button_flow(workspace: Any, *, name: str) -> Flow:
    """A published flow that asks a question and branches on the answer."""
    return published_flow(
        workspace,
        graph(
            [
                node(
                    "ask",
                    "send_message",
                    {
                        "blocks": [{"type": "text", "text": "Small or large?"}],
                        "buttons": [
                            {"id": "small", "label": "Small", "action": "postback"},
                            {"id": "large", "label": "Large", "action": "postback"},
                        ],
                    },
                ),
                node("small", "send_message", {"blocks": [{"type": "text", "text": "One small."}]}, x=200),
                node("large", "send_message", {"blocks": [{"type": "text", "text": "One large."}]}, x=400),
            ],
            [edge("ask", "btn:small", "small"), edge("ask", "btn:large", "large")],
        ),
        name=name,
    )


@pytest.fixture(autouse=True)
def real_pipeline() -> Any:
    """Put the *real* contract-6 processors back for the duration of a test.

    ``apps/channels/tests/conftest.py`` clears the seam for every test in this
    app, deliberately — most of them are about the seam itself and should not run
    a delivery through the whole messaging spine. These tests are the exception:
    they are the acceptance tests, and their whole claim is that the production
    path works end to end. So they re-register persistence and L4-A's routing tail
    by name, inside the conftest's clear-and-restore, which puts everything back
    afterwards.
    """
    from apps.channels import ingest as channels_ingest
    from apps.flows.triggers.pipeline import ROUTING_PROCESSOR, route_events
    from apps.messaging.ingest import PERSISTENCE_PROCESSOR, persist_events

    channels_ingest.register_processor(persist_events, name=PERSISTENCE_PROCESSOR)
    channels_ingest.register_processor(route_events, name=ROUTING_PROCESSOR)
    return None


def sent_texts(graph_fake: Any) -> list[str]:
    """Every text this deployment actually put on the wire."""
    texts = []
    for body in graph_fake.bodies("/messages"):
        message = body.get("message") or {}
        if "text" in message:
            texts.append(message["text"])
        attachment = message.get("attachment") or {}
        payload = attachment.get("payload") or {}
        if "text" in payload:
            texts.append(payload["text"])
    return texts


class TestRefUrlRoundTrip:
    """SPEC §10: ``m.me/<page>?ref=<ref>`` starts the flow bound to that ref."""

    @pytest.fixture
    def ref_trigger(self, tenancy: Tenancy, page: ChannelConnection) -> Trigger:
        flow = one_message_flow(tenancy.workspace, "Here is your spring discount.", name="Spring sale")
        return create_trigger(
            flow,
            trigger_type=TriggerType.REF_URL,
            config={"ref": "spring-sale"},
            connection=page,
        )

    def test_a_referral_starts_the_flow_and_sends_its_first_message(
        self, client: Client, page: ChannelConnection, ref_trigger: Trigger
    ) -> None:
        with fake_graph() as calls:
            response = post_webhook(client, load_delivery("referral"))
        assert response.status_code == 200

        execution = FlowExecution.objects.unscoped().get()
        assert execution.flow_version.flow_id == ref_trigger.flow_id
        assert sent_texts(calls) == ["Here is your spring discount."]

    def test_a_ref_inside_the_get_started_postback_starts_the_same_flow(
        self, client: Client, page: ChannelConnection, ref_trigger: Trigger
    ) -> None:
        """First contact from an m.me link arrives as a postback carrying the ref."""
        with fake_graph() as calls:
            post_webhook(client, load_delivery("postback_with_referral"))
        assert sent_texts(calls) == ["Here is your spring discount."]

    def test_a_different_ref_matches_nothing(
        self, client: Client, page: ChannelConnection, ref_trigger: Trigger
    ) -> None:
        payload = load_delivery("referral")
        payload["entry"][0]["messaging"][0]["referral"]["ref"] = "autumn-sale"
        with fake_graph() as calls:
            post_webhook(client, payload)
        assert sent_texts(calls) == []
        assert not FlowExecution.objects.unscoped().exists()

    def test_a_referral_never_resumes_a_waiting_execution(
        self, client: Client, tenancy: Tenancy, page: ChannelConnection, ref_trigger: Trigger
    ) -> None:
        """``stages.REPLY_EVENTS`` excludes referrals, deliberately.

        A ref handed to a waiting execution matches no button, falls into the
        retry path, and is swallowed by a retry prompt — burning a retry the
        person never used and losing the link they just clicked.
        """
        waiting = button_flow(tenancy.workspace, name="Size")
        create_trigger(
            waiting,
            trigger_type=TriggerType.KEYWORD,
            config={"keywords": [{"text": "order", "mode": "exact"}]},
            connection=page,
        )
        with fake_graph():
            post_webhook(client, _text_delivery("order", mid="m_order"))
        execution = FlowExecution.objects.unscoped().get()
        assert execution.status in LIVE_STATUSES

        with fake_graph() as calls:
            post_webhook(client, load_delivery("referral"))
        # The waiting execution is untouched and the ref started its own flow.
        assert sent_texts(calls) == ["Here is your spring discount."]


class TestWelcome:
    def test_get_started_fires_the_welcome_flow(
        self, client: Client, tenancy: Tenancy, page: ChannelConnection
    ) -> None:
        flow = one_message_flow(tenancy.workspace, "Welcome to Acme.", name="Welcome")
        create_trigger(flow, trigger_type=TriggerType.WELCOME, config={}, connection=page)

        with fake_graph() as calls:
            response = post_webhook(client, load_delivery("postback_get_started"))
        assert response.status_code == 200
        assert sent_texts(calls) == ["Welcome to Acme."]

    def test_the_payload_the_connect_flow_configures_is_the_one_that_matches(self) -> None:
        """The Get Started button and the welcome matcher have to agree.

        They are set in two different modules — ``messenger.GET_STARTED_PAYLOAD``
        at connect time, ``matching.WELCOME_POSTBACKS`` at match time — and a
        disagreement would be a welcome trigger that never fires, with nothing in
        the product able to say why.
        """
        from apps.channels.providers.messenger import GET_STARTED_PAYLOAD
        from apps.flows.triggers.matching import WELCOME_POSTBACKS

        assert GET_STARTED_PAYLOAD in WELCOME_POSTBACKS


class TestButtonsRoundTrip:
    def test_a_pressed_button_resumes_the_execution(
        self, client: Client, tenancy: Tenancy, page: ChannelConnection
    ) -> None:
        flow = button_flow(tenancy.workspace, name="Size")
        create_trigger(
            flow,
            trigger_type=TriggerType.KEYWORD,
            config={"keywords": [{"text": "order", "mode": "exact"}]},
            connection=page,
        )

        with fake_graph() as calls:
            post_webhook(client, _text_delivery("order", mid="m_order"))
        assert "Small or large?" in sent_texts(calls)
        execution = FlowExecution.objects.unscoped().get()

        with fake_graph() as calls:
            post_webhook(client, _postback_delivery("ask:large", mid="m_press"))
        assert sent_texts(calls) == ["One large."]
        execution.refresh_from_db()
        assert execution.status not in LIVE_STATUSES

    def test_a_quick_reply_resumes_it_too(self, client: Client, tenancy: Tenancy, page: ChannelConnection) -> None:
        """Meta delivers a tapped chip as a message; the adapter makes it a postback."""
        flow = button_flow(tenancy.workspace, name="Size")
        create_trigger(
            flow,
            trigger_type=TriggerType.KEYWORD,
            config={"keywords": [{"text": "order", "mode": "exact"}]},
            connection=page,
        )
        with fake_graph():
            post_webhook(client, _text_delivery("order", mid="m_order"))

        payload = load_delivery("message_quick_reply")
        payload["entry"][0]["messaging"][0]["message"]["quick_reply"]["payload"] = "ask:small"
        with fake_graph() as calls:
            post_webhook(client, payload)
        assert sent_texts(calls) == ["One small."]


class TestReceipts:
    """Delivery and read receipts move ``message.status`` — through the facade only."""

    @pytest.fixture
    def sent_message(self, client: Client, tenancy: Tenancy, page: ChannelConnection) -> Message:
        flow = one_message_flow(tenancy.workspace, "Your order is on its way.", name="Update")
        create_trigger(flow, trigger_type=TriggerType.WELCOME, config={}, connection=page)
        with fake_graph():
            post_webhook(client, load_delivery("postback_get_started"))
        return Message.objects.unscoped().get(direction=MessageDirection.OUT)

    def test_a_send_records_the_provider_id(self, sent_message: Message) -> None:
        assert sent_message.status == MessageStatus.SENT
        assert sent_message.provider_message_id == "mid.out-1"

    def test_a_delivery_receipt_advances_the_status(
        self, client: Client, page: ChannelConnection, sent_message: Message
    ) -> None:
        with fake_graph():
            response = post_webhook(client, load_delivery("delivery"))
        assert response.status_code == 200
        sent_message.refresh_from_db()
        assert sent_message.status == MessageStatus.DELIVERED

    def test_a_read_watermark_is_resolved_to_our_own_messages(
        self, client: Client, page: ChannelConnection, sent_message: Message
    ) -> None:
        """Meta's read receipt names no message; it names a moment.

        So the adapter resolves the watermark to the ids of our own recent
        outbound messages on this connection — read-only, scoped and bounded — and
        the status still moves only through ``apps.messaging.ingest``.
        """
        with fake_graph():
            post_webhook(client, _read_delivery())
        sent_message.refresh_from_db()
        assert sent_message.status == MessageStatus.READ

    def test_a_watermark_older_than_the_message_resolves_to_nothing(
        self, client: Client, page: ChannelConnection, sent_message: Message
    ) -> None:
        """A read that predates the send cannot be a read *of* it.

        The recorded fixture's watermark is a fixed moment in the past, which is
        exactly this case — and answering "nothing" is the direction to fail in.
        """
        with fake_graph():
            response = post_webhook(client, load_delivery("read"))
        assert response.status_code == 200
        sent_message.refresh_from_db()
        assert sent_message.status == MessageStatus.SENT

    def test_a_late_delivery_receipt_never_un_reads_a_message(
        self, client: Client, page: ChannelConnection, sent_message: Message
    ) -> None:
        """Platforms do not promise receipt ordering. The ladder only moves forward."""
        with fake_graph():
            post_webhook(client, _read_delivery())
        sent_message.refresh_from_db()
        assert sent_message.status == MessageStatus.READ

        with fake_graph():
            post_webhook(client, load_delivery("delivery"))
        sent_message.refresh_from_db()
        assert sent_message.status == MessageStatus.READ

    def test_one_persons_read_receipt_does_not_mark_anothers_messages(
        self, client: Client, tenancy: Tenancy, page: ChannelConnection, sent_message: Message
    ) -> None:
        """A page talks to thousands of people at once.

        The watermark says "everything *this person* was sent has been read", so a
        connection-wide resolution would mark one contact's unread messages read
        because a different contact opened Messenger.
        """
        payload = _read_delivery()
        payload["entry"][0]["messaging"][0]["sender"]["id"] = "999888777666555"
        with fake_graph():
            post_webhook(client, payload)
        sent_message.refresh_from_db()
        assert sent_message.status == MessageStatus.SENT

    def test_a_receipt_for_a_message_we_never_sent_is_ignored(
        self, client: Client, page: ChannelConnection, sent_message: Message
    ) -> None:
        payload = load_delivery("delivery")
        payload["entry"][0]["messaging"][0]["delivery"]["mids"] = ["mid.someone-elses"]
        with fake_graph():
            response = post_webhook(client, payload)
        assert response.status_code == 200
        sent_message.refresh_from_db()
        assert sent_message.status == MessageStatus.SENT

    def test_a_read_watermark_only_covers_this_connections_messages(
        self, client: Client, tenancy: Tenancy, page: ChannelConnection, sent_message: Message
    ) -> None:
        """The resolution query is scoped to the workspace and to this connection."""
        from apps.channels.providers import meta_common

        other = ChannelConnection(
            workspace=tenancy.workspace,
            platform=page.platform,
            display_name="Second page",
            external_id="444444444444444",
        )
        meta_common.store_page_token(other, "EAAsecondpagetoken0123456789abcdef")
        other.rotate_webhook_secret()
        other.save()

        payload = load_delivery("read")
        payload["entry"][0]["id"] = other.external_id
        with fake_graph():
            post_webhook(client, payload)
        sent_message.refresh_from_db()
        assert sent_message.status == MessageStatus.SENT


class TestTheSignatureIsTheCredential:
    def test_a_wrong_signature_never_reaches_the_flow(
        self, client: Client, tenancy: Tenancy, page: ChannelConnection
    ) -> None:
        flow = one_message_flow(tenancy.workspace, "Welcome to Acme.", name="Welcome")
        create_trigger(flow, trigger_type=TriggerType.WELCOME, config={}, connection=page)

        with fake_graph() as calls:
            response = post_webhook(client, load_delivery("postback_get_started"), secret="the-wrong-secret")
        assert response.status_code == 403
        assert calls.calls == []
        assert not FlowExecution.objects.unscoped().exists()

    def test_a_missing_signature_header_is_refused(self, client: Client, page: ChannelConnection) -> None:
        response = client.post(
            "/webhooks/messenger/",
            data=json.dumps(load_delivery("message_text")).encode(),
            content_type="application/json",
        )
        assert response.status_code == 403

    def test_a_deployment_with_no_app_secret_refuses_every_delivery(
        self, client: Client, page: ChannelConnection, settings: Any
    ) -> None:
        """Fails closed: with no secret we cannot tell a real delivery from a forged one."""
        settings.PLATFORM_CREDENTIALS_FROM_ENV = {}
        response = post_webhook(client, load_delivery("message_text"))
        assert response.status_code == 403

    def test_a_redelivered_message_is_processed_once(
        self, client: Client, tenancy: Tenancy, page: ChannelConnection
    ) -> None:
        flow = one_message_flow(tenancy.workspace, "Welcome to Acme.", name="Welcome")
        create_trigger(flow, trigger_type=TriggerType.WELCOME, config={}, connection=page)

        with fake_graph() as first:
            post_webhook(client, load_delivery("postback_get_started"))
        with fake_graph() as second:
            post_webhook(client, load_delivery("postback_get_started"))
        assert sent_texts(first) == ["Welcome to Acme."]
        assert sent_texts(second) == []


class TestHubChallenge:
    """Meta's GET verification, on the same URL (SPEC §7.1)."""

    def test_the_configured_verify_token_is_echoed(self, client: Client, app_secret: str) -> None:
        response = client.get(
            "/webhooks/messenger/",
            {"hub.mode": "subscribe", "hub.verify_token": "fake-verify-token", "hub.challenge": "1234567890"},
        )
        assert response.status_code == 200
        assert response.content == b"1234567890"

    def test_a_wrong_verify_token_is_refused(self, client: Client, app_secret: str) -> None:
        response = client.get(
            "/webhooks/messenger/",
            {"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "1234567890"},
        )
        assert response.status_code == 403


# ---------------------------------------------------------------------------
# Payload builders
# ---------------------------------------------------------------------------


def _text_delivery(text: str, *, mid: str) -> dict[str, Any]:
    payload = load_delivery("message_text")
    payload["entry"][0]["messaging"][0]["message"] = {"mid": mid, "text": text}
    return payload


def _read_delivery() -> dict[str, Any]:
    """A read receipt whose watermark is *now*, which is what a real one carries.

    The recorded fixture holds a fixed moment in the past, so it is the right
    payload for the shape and the wrong one for the semantics — a receipt always
    arrives after the message it refers to.
    """
    payload = load_delivery("read")
    now_ms = int(timezone.now().timestamp() * 1000) + 1000
    payload["entry"][0]["messaging"][0]["read"]["watermark"] = now_ms
    payload["entry"][0]["messaging"][0]["timestamp"] = now_ms
    return payload


def _postback_delivery(button_payload: str, *, mid: str) -> dict[str, Any]:
    payload = load_delivery("postback_button")
    payload["entry"][0]["messaging"][0]["postback"] = {"title": "Large", "payload": button_payload}
    payload["entry"][0]["messaging"][0]["timestamp"] = 1712345695000
    return payload
