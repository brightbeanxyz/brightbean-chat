"""Preview beyond Telegram.

``test_telegram_preview.py`` stays as the Telegram regression suite and now
reaches the view through the general route. This one covers what generalising it
added: the two other platforms that can carry a ref, the channel it chooses, and
the two honest empty states.
"""

from collections.abc import Iterator
from typing import Any

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.channels import ingest as channels_ingest
from apps.channels import preview
from apps.channels.events import EventPayload, EventType, NormalizedEvent
from apps.channels.models import ChannelConnection, ConnectionStatus
from apps.common.platforms import Platform
from apps.flows.models import Trigger, TriggerType
from apps.flows.services import create_flow, publish, save_draft
from apps.members.roles import WorkspaceRole
from tests.support import Tenancy

pytestmark = pytest.mark.django_db

CHAT = "psid-1"


def one_message_graph(text: str) -> dict[str, Any]:
    return {
        "schema": 1,
        "nodes": [
            {
                "id": "n1",
                "type": "send_message",
                "position": {"x": 0, "y": 0},
                "config": {"blocks": [{"type": "text", "text": text}]},
            }
        ],
        "edges": [],
    }


@pytest.fixture(autouse=True)
def pipeline() -> Iterator[None]:
    """Persistence then preview, as production composes them. See the sibling
    suite for why calling the stage directly would hide the coupling."""
    from apps.messaging.ingest import PERSISTENCE_PROCESSOR, persist_events

    channels_ingest.register_processor(persist_events, name=PERSISTENCE_PROCESSOR)
    channels_ingest.register_processor(preview.preview_events, name=preview.PREVIEW_PROCESSOR)
    yield


@pytest.fixture
def drafted_flow(tenancy: Tenancy) -> Any:
    flow = create_flow(workspace=tenancy.workspace, name="Welcome")
    save_draft(flow, one_message_graph("PUBLISHED"))
    publish(flow)
    save_draft(flow, one_message_graph("DRAFT"))
    flow.refresh_from_db()
    return flow


def connection_for(tenancy: Tenancy, platform: str, *, display_name: str, external_id: str) -> ChannelConnection:
    connection = ChannelConnection(
        workspace=tenancy.workspace,
        platform=platform,
        display_name=display_name,
        external_id=external_id,
        status=ConnectionStatus.ACTIVE,
    )
    connection.rotate_webhook_secret()
    connection.save()
    return connection


def bind(flow: Any, connection: ChannelConnection) -> Trigger:
    trigger = Trigger(
        flow=flow,
        channel_connection=connection,
        type=TriggerType.KEYWORD,
        config_json={"keywords": [{"text": "hi", "mode": "contains"}]},
    )
    trigger.save()
    return trigger


def deliver(connection: ChannelConnection, ref: str, chat_id: str = CHAT) -> None:
    channels_ingest.process_events(
        connection,
        (
            NormalizedEvent(
                type=EventType.REFERRAL,
                connection=connection,
                platform_user_id=chat_id,
                provider_event_id=f"{connection.platform}:{ref}:{chat_id}",
                timestamp=timezone.now(),
                payload=EventPayload(ref=ref),
            ),
        ),
    )


def url(tenancy: Tenancy, flow: Any) -> str:
    return reverse("channels:flow_preview", kwargs={"workspace_id": tenancy.workspace.pk, "flow_id": flow.pk})


class TestTheMetaPlatformsRunTheDraft:
    """The ingest half. Everything downstream of the link was already
    platform-agnostic — ``_claim`` binds on the connection and ``chat_id``
    holds a PSID or an IGSID as happily as a Telegram chat id — so what these
    prove is that dropping the Telegram gate was all it took."""

    @pytest.mark.parametrize("platform", [Platform.MESSENGER, Platform.INSTAGRAM])
    def test_a_ref_starts_the_draft(self, tenancy: Tenancy, drafted_flow: Any, platform: str) -> None:
        connection = connection_for(tenancy, platform, display_name="@acme", external_id=f"{platform}-page")
        _link, handle = preview.mint(flow=drafted_flow, connection=connection, user=tenancy.owner)

        deliver(connection, preview.start_payload(handle))

        from apps.flows.models import FlowExecution

        execution = FlowExecution.objects.for_workspace(tenancy.workspace).first()
        assert execution is not None
        assert execution.flow_version.published is False

    def test_a_link_minted_for_one_page_does_not_work_on_another(self, tenancy: Tenancy, drafted_flow: Any) -> None:
        """The security-relevant clause in ``_claim``, re-pinned for Meta: the
        lookup at claim time spans every connection in the deployment, because
        an inbound webhook has no session and therefore no workspace."""
        minted_for = connection_for(tenancy, Platform.MESSENGER, display_name="Acme", external_id="page-1")
        other = connection_for(tenancy, Platform.MESSENGER, display_name="Other", external_id="page-2")
        _link, handle = preview.mint(flow=drafted_flow, connection=minted_for, user=tenancy.owner)

        deliver(other, preview.start_payload(handle))

        from apps.flows.models import FlowExecution

        assert not FlowExecution.objects.for_workspace(tenancy.workspace).exists()


class TestItOffersTheChannelTheFlowIsBuiltFor:
    def test_an_instagram_flow_is_not_handed_a_telegram_link(
        self, tenancy: Tenancy, client_for: Any, drafted_flow: Any
    ) -> None:
        """The whole point. A workspace can have a Telegram bot connected and
        still be building an Instagram automation."""
        connection_for(tenancy, Platform.TELEGRAM, display_name="@acme_bot", external_id="tg-1")
        instagram = connection_for(tenancy, Platform.INSTAGRAM, display_name="@acme", external_id="ig-1")
        bind(drafted_flow, instagram)

        payload = client_for(tenancy.user_for(WorkspaceRole.EDITOR)).post(url(tenancy, drafted_flow)).json()

        assert payload["ok"] is True
        assert payload["platform"] == Platform.INSTAGRAM
        assert payload["deep_link"].startswith("https://ig.me/m/acme?ref=preview-")
        assert payload["account"] == "@acme"

    def test_messenger_links_by_page_id(self, tenancy: Tenancy, client_for: Any, drafted_flow: Any) -> None:
        """Messenger needs no handle resolver: its external_id *is* the page id
        and ``m.me/<page-id>`` resolves."""
        connection = connection_for(tenancy, Platform.MESSENGER, display_name="Acme Inc", external_id="123456")
        bind(drafted_flow, connection)

        payload = client_for(tenancy.user_for(WorkspaceRole.EDITOR)).post(url(tenancy, drafted_flow)).json()

        assert payload["deep_link"].startswith("https://m.me/123456?ref=preview-")
        assert payload["account"] == "Acme Inc"

    def test_the_meta_platforms_say_a_tap_is_not_enough(
        self, tenancy: Tenancy, client_for: Any, drafted_flow: Any
    ) -> None:
        """Telegram delivers /start on the tap; Meta opens a composer and the
        referral rides in with the first message. Without saying so, a working
        link gets reported as broken."""
        connection = connection_for(tenancy, Platform.INSTAGRAM, display_name="@acme", external_id="ig-1")
        bind(drafted_flow, connection)

        payload = client_for(tenancy.user_for(WorkspaceRole.EDITOR)).post(url(tenancy, drafted_flow)).json()

        assert "Send any message" in payload["instructions"]

    def test_telegram_needs_no_instructions(self, tenancy: Tenancy, client_for: Any, drafted_flow: Any) -> None:
        from apps.channels.providers.telegram import store_bot_token

        connection = connection_for(tenancy, Platform.TELEGRAM, display_name="@acme_bot", external_id="tg-1")
        store_bot_token(connection, "123456:abcdefghijklmnopqrstuvwxyzABCDEFGHI")
        connection.save()
        bind(drafted_flow, connection)

        payload = client_for(tenancy.user_for(WorkspaceRole.EDITOR)).post(url(tenancy, drafted_flow)).json()

        assert payload["instructions"] == ""


class TestTheHonestEmptyStates:
    def test_a_channel_with_no_live_test_is_explained_not_an_error(
        self, tenancy: Tenancy, client_for: Any, drafted_flow: Any
    ) -> None:
        """WhatsApp emits no referral event and has no column this app can trust
        to hold a dialable public handle. Saying so is a better answer than a
        token the tester has to paste into a message body."""
        connection = connection_for(tenancy, Platform.WHATSAPP, display_name="+15550001111", external_id="wa-1")
        bind(drafted_flow, connection)

        response = client_for(tenancy.user_for(WorkspaceRole.EDITOR)).post(url(tenancy, drafted_flow))
        payload = response.json()

        assert response.status_code == 200
        assert payload["ok"] is False
        assert payload["reason"] == "unsupported_platform"
        assert "WhatsApp" in payload["message"]
        # Nothing to go and connect would change the answer.
        assert "settings_url" not in payload

    def test_an_untestable_flow_does_not_report_a_missing_connection(
        self, tenancy: Tenancy, client_for: Any, drafted_flow: Any
    ) -> None:
        """ "Connect SMS first" would be advice that cannot help: connecting one
        does not make a live test exist."""
        connection = connection_for(tenancy, Platform.SMS, display_name="+15550001111", external_id="sms-1")
        bind(drafted_flow, connection)

        payload = client_for(tenancy.user_for(WorkspaceRole.EDITOR)).post(url(tenancy, drafted_flow)).json()

        assert payload["reason"] == "unsupported_platform"

    def test_a_workspace_with_nothing_connected_is_told_to_connect_something(
        self, tenancy: Tenancy, client_for: Any, drafted_flow: Any
    ) -> None:
        """platforms_for_flow answers with the workspace's connected platforms
        when the flow names none, so on an empty workspace it answers with
        nothing — which is "cannot tell", not "no live test exists". Conflating
        the two told a brand-new workspace its flow was untestable."""
        payload = client_for(tenancy.user_for(WorkspaceRole.EDITOR)).post(url(tenancy, drafted_flow)).json()

        assert payload["reason"] == "no_connection"
        assert payload["settings_url"]

    def test_a_viewer_may_not_mint_one(self, tenancy: Tenancy, client_for: Any, drafted_flow: Any) -> None:
        response = client_for(tenancy.user_for(WorkspaceRole.VIEWER)).post(url(tenancy, drafted_flow))

        assert response.status_code == 403

    def test_another_tenants_flow_is_404_not_403(
        self, tenancy: Tenancy, other_tenancy: Tenancy, client_for: Any, drafted_flow: Any
    ) -> None:
        stolen = reverse(
            "channels:flow_preview",
            kwargs={"workspace_id": other_tenancy.workspace.pk, "flow_id": drafted_flow.pk},
        )

        assert client_for(other_tenancy.owner).post(stolen).status_code == 404
