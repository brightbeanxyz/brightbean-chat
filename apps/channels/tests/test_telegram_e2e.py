"""The acceptance test: a real webhook in, a real Bot API call out.

    End-to-end integration test (fake Telegram server): /start -> welcome
    trigger fires published flow -> button press resumes via callback query ->
    429 retry honors retry_after.

Everything between the two ends is the production path — the webhook endpoint,
signature verification, deduplication, the contract-6 seam, L3-A's persistence
and compliance and token bucket, L3-B's engine, and this adapter. The only
substitutions are the network (``httpx.MockTransport``) and the router.

--------------------------------------------------------------------------
The router here is a stand-in, and is meant to be replaced
--------------------------------------------------------------------------

L4-A (#11) owns the routing tail and has not merged: ``apps/messaging/ingest.py``
still registers a documented **no-op** under ``ROUTING_PROCESSOR``. So
:func:`routing_stand_in` below registers under that same name and does the two
stages this test needs, in L4-A's order — ``resume`` before ``trigger`` — using
the same engine entry points the real router is specified to call
(``attempt_resume``, then ``start_flow``).

It is deliberately the smallest thing that can be called a router: no priority
ordering, no keyword matching, no default reply, no frequency guard. Its only
job is to let the *adapter* be tested end to end before its sibling lands.

**On the rebase this branch owes before merging** — the layer's merge order puts
#11 first — this fixture goes and the test runs against the real router. If you
are reading this after #11 merged and it is still here, that rebase did not
happen.
"""

import json
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from django.test import Client
from django.utils import timezone

from apps.channels import ingest as channels_ingest
from apps.channels.events import EventType
from apps.channels.models import ChannelConnection
from apps.channels.providers.telegram import SECRET_HEADER
from apps.channels.tests.telegram_support import BOT_TOKEN, Reply, fake_bot_api, load_update
from apps.common.platforms import Platform
from apps.flows.engine import Consumed, attempt_resume, start_flow
from apps.flows.models import LIVE_STATUSES, Flow, FlowExecution, StartedBy
from apps.flows.tests.support import edge, graph, node
from apps.messaging.ingest import PERSISTENCE_PROCESSOR, ROUTING_PROCESSOR, persist_events
from apps.messaging.models import Message, MessageDirection, MessageStatus
from apps.queueing.models import ScheduledAction
from tests.support import Tenancy

pytestmark = pytest.mark.django_db

WEBHOOK_URL = "/webhooks/telegram/"
CHAT = "5150"

#: A flow that greets, offers two buttons, and says something different for each.
BUTTON_FLOW = graph(
    [
        node(
            "ask",
            "send_message",
            {
                "blocks": [{"type": "text", "text": "Welcome! Are you a customer?"}],
                "buttons": [
                    {"id": "yes", "label": "Yes", "action": "postback"},
                    {"id": "no", "label": "No", "action": "postback"},
                ],
            },
        ),
        node("said_yes", "send_message", {"blocks": [{"type": "text", "text": "Great to have you back."}]}, x=200),
        node("said_no", "send_message", {"blocks": [{"type": "text", "text": "Welcome aboard."}]}, x=400),
    ],
    [edge("ask", "btn:yes", "said_yes"), edge("ask", "btn:no", "said_no")],
)


def post(client: Client, update: dict[str, Any], *, secret: str) -> Any:
    body = json.dumps(update).encode()
    return client.post(WEBHOOK_URL, data=body, content_type="application/json", headers={SECRET_HEADER: secret})


@pytest.fixture
def telegram_connection(tenancy: Tenancy) -> ChannelConnection:
    connection = ChannelConnection(
        workspace=tenancy.workspace,
        platform=Platform.TELEGRAM,
        display_name="@acme_bot",
        external_id="777000",
        credentials={"bot_token": BOT_TOKEN},
    )
    connection.rotate_webhook_secret()
    connection.save()
    return connection


@pytest.fixture
def bot_secret(telegram_connection: ChannelConnection) -> str:
    return telegram_connection.webhook_secret


@pytest.fixture
def welcome_flow(tenancy: Tenancy) -> Flow:
    from apps.flows.services import create_flow, publish, save_draft

    flow = create_flow(workspace=tenancy.workspace, name="Welcome")
    save_draft(flow, BUTTON_FLOW)
    publish(flow)
    flow.refresh_from_db()
    return flow


@pytest.fixture(autouse=True)
def routing_stand_in(welcome_flow: Flow) -> Iterator[None]:
    """L4-A's tail, reduced to resume-then-trigger. See the module docstring."""

    def route(connection: Any, events: Any) -> None:
        for event in events:
            contact = _contact_for(connection, event.platform_user_id)
            if contact is None:
                continue

            # Stage 1: resume. A live execution gets first refusal on the event.
            live = FlowExecution.objects.unscoped().filter(contact=contact, status__in=LIVE_STATUSES).first()
            if live is not None and isinstance(attempt_resume(live, event), Consumed):
                continue

            # Stage 2: trigger. Only the welcome trigger exists here — bare
            # /start, which the adapter flags in payload.extra.
            if event.type == EventType.MESSAGE and event.payload.extra.get("command") == "start":
                start_flow(
                    contact,
                    welcome_flow,
                    started_by=StartedBy.stamp(StartedBy.TRIGGER, "welcome"),
                    connection=connection,
                )

    channels_ingest.register_processor(persist_events, name=PERSISTENCE_PROCESSOR)
    channels_ingest.register_processor(route, name=ROUTING_PROCESSOR)
    yield


def _contact_for(connection: ChannelConnection, platform_user_id: str) -> Any:
    from apps.messaging.models import ContactChannelIdentity

    identity = (
        ContactChannelIdentity.objects.unscoped()
        .filter(channel_connection=connection, platform_user_id=platform_user_id)
        .select_related("contact")
        .first()
    )
    return identity.contact if identity else None


def start_update(update_id: int = 900002) -> dict[str, Any]:
    update = load_update("message_start_bare")
    update["update_id"] = update_id
    return update


def press(button_id: str, node_id: str = "ask", update_id: int = 900010) -> dict[str, Any]:
    update = load_update("callback_query")
    update["update_id"] = update_id
    update["callback_query"]["data"] = f"{node_id}:{button_id}"
    return update


class TestTheWholeLoop:
    def test_start_fires_the_flow_and_a_button_press_resumes_it(
        self, client: Client, tenancy: Tenancy, telegram_connection: ChannelConnection, bot_secret: str
    ) -> None:
        with fake_bot_api() as fake:
            assert post(client, start_update(), secret=bot_secret).status_code == 200

            # The welcome trigger fired the published flow, and the first node's
            # message went out with the buttons as an inline keyboard.
            (greeting,) = fake.payloads("sendMessage")
            assert greeting["chat_id"] == CHAT
            assert greeting["text"] == "Welcome! Are you a customer?"
            assert greeting["reply_markup"]["inline_keyboard"] == [
                [{"text": "Yes", "callback_data": "ask:yes"}],
                [{"text": "No", "callback_data": "ask:no"}],
            ]

            execution = FlowExecution.objects.unscoped().get()
            assert execution.current_node_id == "ask"
            assert execution.preview is False

            # The contact presses "Yes". callback_data comes back as the button
            # id, the waiting execution consumes it, and the flow advances.
            assert post(client, press("yes"), secret=bot_secret).status_code == 200

        assert fake.payloads("sendMessage")[-1]["text"] == "Great to have you back."
        execution.refresh_from_db()
        assert execution.current_node_id == "said_yes"
        # And the spinner on the pressed button was cleared.
        assert "answerCallbackQuery" in fake.methods()

    def test_the_other_button_takes_the_other_edge(
        self, client: Client, tenancy: Tenancy, telegram_connection: ChannelConnection, bot_secret: str
    ) -> None:
        with fake_bot_api() as fake:
            post(client, start_update(), secret=bot_secret)
            post(client, press("no"), secret=bot_secret)
        assert fake.payloads("sendMessage")[-1]["text"] == "Welcome aboard."

    def test_a_redelivered_update_is_processed_once(
        self, client: Client, tenancy: Tenancy, telegram_connection: ChannelConnection, bot_secret: str
    ) -> None:
        """Telegram retries a delivery it did not get a 200 for. update_id is
        the dedup key (SPEC §7.1 step 2), so the second one does nothing."""
        with fake_bot_api() as fake:
            post(client, start_update(), secret=bot_secret)
            post(client, start_update(), secret=bot_secret)
        assert len(fake.payloads("sendMessage")) == 1
        assert FlowExecution.objects.unscoped().count() == 1

    def test_a_delivery_with_the_wrong_secret_never_reaches_the_flow(
        self, client: Client, tenancy: Tenancy, telegram_connection: ChannelConnection
    ) -> None:
        with fake_bot_api() as fake:
            response = post(client, start_update(), secret="not-the-secret")
        assert response.status_code == 403
        assert fake.calls == []
        assert not FlowExecution.objects.unscoped().exists()


class TestRateLimiting:
    def test_a_429_reschedules_the_message_for_retry_after_seconds(
        self, client: Client, tenancy: Tenancy, telegram_connection: ChannelConnection, bot_secret: str
    ) -> None:
        """SPEC §6.2: "on HTTP 429 honor retry_after and reschedule". The flow
        must not die and the message must not be marked failed — it is queued,
        with a retry armed for when Telegram said to come back."""
        throttled = Reply(
            status=429,
            body={
                "ok": False,
                "error_code": 429,
                "description": "Too Many Requests: retry after 120",
                "parameters": {"retry_after": 120},
            },
        )
        before = timezone.now()
        with fake_bot_api(lambda fake: fake.reply("sendMessage", throttled)):
            assert post(client, start_update(), secret=bot_secret).status_code == 200

        # Outbound only: persistence also wrote the contact's own /start into
        # the thread, which is a Message row too.
        message = Message.objects.unscoped().get(direction=MessageDirection.OUT)
        assert message.status == MessageStatus.QUEUED
        assert message.error == "rate_limited"

        action = ScheduledAction.objects.unscoped().filter(type="send_retry").get()
        # Telegram's number, not our backoff ladder.
        assert action.run_at >= before + timedelta(seconds=110)

        # The execution is still alive and still at the node that was sending,
        # so the retry resumes rather than restarting.
        execution = FlowExecution.objects.unscoped().get()
        assert execution.status in LIVE_STATUSES

    def test_the_retry_goes_out_when_it_runs(
        self, client: Client, tenancy: Tenancy, telegram_connection: ChannelConnection, bot_secret: str
    ) -> None:
        """The reschedule is only worth anything if the message actually sends
        on the next attempt."""
        throttled = Reply(status=429, body={"ok": False, "error_code": 429, "parameters": {"retry_after": 1}})
        with fake_bot_api(lambda fake: fake.reply("sendMessage", throttled)):
            post(client, start_update(), secret=bot_secret)

        from apps.messaging.handlers import handle_send_retry

        action = ScheduledAction.objects.unscoped().filter(type="send_retry").get()
        with fake_bot_api() as fake:
            handle_send_retry(action.payload, action)

        assert fake.payloads("sendMessage")[0]["text"] == "Welcome! Are you a customer?"
        message = Message.objects.unscoped().get(direction=MessageDirection.OUT)
        assert message.status == MessageStatus.SENT

    def test_the_retry_keeps_the_inline_keyboard(
        self, client: Client, tenancy: Tenancy, telegram_connection: ChannelConnection, bot_secret: str
    ) -> None:
        """A retry rebuilds the message from its stored row hours later. Without
        node_id in that row the second attempt's callback_data would differ from
        the first — two live keyboards in one chat, answering differently."""
        throttled = Reply(status=429, body={"ok": False, "error_code": 429, "parameters": {"retry_after": 1}})
        with fake_bot_api(lambda fake: fake.reply("sendMessage", throttled)) as first:
            post(client, start_update(), secret=bot_secret)
        attempted = first.payloads("sendMessage")[0]["reply_markup"]

        from apps.messaging.handlers import handle_send_retry

        action = ScheduledAction.objects.unscoped().filter(type="send_retry").get()
        with fake_bot_api() as second:
            handle_send_retry(action.payload, action)

        assert second.payloads("sendMessage")[0]["reply_markup"] == attempted
