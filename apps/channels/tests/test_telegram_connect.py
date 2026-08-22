"""The guided connect flow, disconnection, and webhook health.

The ordering assertions are the point of most of this. ``getMe`` before any
write, because a token that does not work should leave no trace and because it
is the only source of the bot's id and username; ``setWebhook`` inside the
transaction, because a bot Telegram will not deliver to is not a connection and
must not sit in the list looking like one.
"""

from typing import Any

import pytest
from django.test import Client
from django.urls import reverse

from apps.channels.models import ChannelConnection, WebhookEventLog
from apps.channels.tests.telegram_support import BOT_TOKEN, Reply, fake_bot_api
from apps.common.platforms import Platform
from apps.members.roles import WorkspaceRole
from tests.support import Tenancy

pytestmark = pytest.mark.django_db

BOT = {"id": 777000, "is_bot": True, "first_name": "Acme", "username": "acme_bot"}


def connect_url(tenancy: Tenancy) -> str:
    return reverse("channels:telegram_connect", kwargs={"workspace_id": tenancy.workspace.pk})


def as_admin(client: Client, tenancy: Tenancy) -> Client:
    """``manage_channels`` is admin-only (apps.members.roles._ADMIN_ONLY_KEYS)."""
    client.force_login(tenancy.user_for(WorkspaceRole.ADMIN))
    return client


class TestConnect:
    def test_a_good_token_creates_a_connection_and_sets_the_webhook(self, client: Client, tenancy: Tenancy) -> None:
        with fake_bot_api(lambda fake: fake.reply("getMe", Reply(result=BOT))) as fake:
            response = as_admin(client, tenancy).post(connect_url(tenancy), {"bot_token": BOT_TOKEN})

        assert response.status_code == 302
        connection = ChannelConnection.objects.for_workspace(tenancy.workspace).get()
        assert connection.platform == Platform.TELEGRAM
        assert connection.external_id == "777000"
        assert connection.display_name == "@acme_bot"
        assert connection.credentials["bot_token"] == BOT_TOKEN

        assert fake.methods() == ["getMe", "setWebhook"]
        (payload,) = fake.payloads("setWebhook")
        # The secret Telegram will echo in X-Telegram-Bot-Api-Secret-Token, and
        # which verify_webhook compares against. Never stored in the clear.
        assert connection.verify_webhook_secret(payload["secret_token"])
        assert payload["url"].endswith("/webhooks/telegram/")
        assert payload["allowed_updates"] == ["message", "callback_query"]
        # A bot connected today must not replay messages sent to it before this
        # workspace existed — they would arrive as events and fire triggers.
        assert payload["drop_pending_updates"] is True

    def test_get_me_runs_before_anything_is_written(self, client: Client, tenancy: Tenancy) -> None:
        with fake_bot_api(lambda fake: fake.reply("getMe", Reply(status=401))) as fake:
            response = as_admin(client, tenancy).post(connect_url(tenancy), {"bot_token": "wrong"})

        assert response.status_code == 200
        assert "Telegram did not accept that token" in response.content.decode()
        assert ChannelConnection.objects.for_workspace(tenancy.workspace).count() == 0
        assert fake.methods() == ["getMe"]

    def test_a_failed_set_webhook_rolls_the_connection_back(self, client: Client, tenancy: Tenancy) -> None:
        """Otherwise the operator has a row that looks connected and never
        receives anything — the worst of both outcomes."""

        def configure(fake: Any) -> None:
            fake.reply("getMe", Reply(result=BOT))
            fake.reply("setWebhook", Reply(status=400))

        with fake_bot_api(configure):
            response = as_admin(client, tenancy).post(connect_url(tenancy), {"bot_token": BOT_TOKEN})

        assert response.status_code == 200
        assert "public HTTPS" in response.content.decode()
        assert ChannelConnection.objects.for_workspace(tenancy.workspace).count() == 0

    def test_an_unusable_get_me_result_is_refused(self, client: Client, tenancy: Tenancy) -> None:
        with fake_bot_api(lambda fake: fake.reply("getMe", Reply(result={"id": "not-an-int"}))):
            response = as_admin(client, tenancy).post(connect_url(tenancy), {"bot_token": BOT_TOKEN})
        assert response.status_code == 200
        assert ChannelConnection.objects.for_workspace(tenancy.workspace).count() == 0

    def test_an_empty_token_never_reaches_telegram(self, client: Client, tenancy: Tenancy) -> None:
        with fake_bot_api() as fake:
            response = as_admin(client, tenancy).post(connect_url(tenancy), {"bot_token": "   "})
        assert response.status_code == 200
        assert fake.calls == []

    def test_a_bot_already_connected_elsewhere_says_so_without_naming_where(
        self, client: Client, tenancy: Tenancy, other_tenancy: Tenancy
    ) -> None:
        """SPEC §5's unique (platform, external_id) is deployment-wide: one bot
        cannot serve two workspaces, or the second silently takes over the
        first's inbound traffic. The message must not reveal which workspace
        holds it (SECURITY-BASELINE §1)."""
        ChannelConnection.objects.create(
            workspace=other_tenancy.workspace,
            platform=Platform.TELEGRAM,
            display_name="@acme_bot",
            external_id="777000",
        )
        with fake_bot_api(lambda fake: fake.reply("getMe", Reply(result=BOT))):
            response = as_admin(client, tenancy).post(connect_url(tenancy), {"bot_token": BOT_TOKEN})

        body = response.content.decode()
        assert "already connected to this deployment" in body
        assert other_tenancy.workspace.name not in body
        assert ChannelConnection.objects.for_workspace(tenancy.workspace).count() == 0

    def test_the_page_never_echoes_the_token_back(self, client: Client, tenancy: Tenancy) -> None:
        with fake_bot_api(lambda fake: fake.reply("getMe", Reply(status=401))):
            response = as_admin(client, tenancy).post(connect_url(tenancy), {"bot_token": BOT_TOKEN})
        assert BOT_TOKEN not in response.content.decode()

    @pytest.mark.parametrize("role", [WorkspaceRole.EDITOR, WorkspaceRole.AGENT, WorkspaceRole.VIEWER])
    def test_only_manage_channels_may_connect(self, client: Client, tenancy: Tenancy, role: str) -> None:
        client.force_login(tenancy.user_for(role))
        assert client.get(connect_url(tenancy)).status_code == 403


class TestDisconnect:
    def test_disconnecting_tells_telegram_to_stop(self, client: Client, tenancy: Tenancy) -> None:
        connection = ChannelConnection.objects.create(
            workspace=tenancy.workspace,
            platform=Platform.TELEGRAM,
            display_name="@acme_bot",
            external_id="777000",
            credentials={"bot_token": BOT_TOKEN},
        )
        url = reverse("channels:delete", kwargs={"workspace_id": tenancy.workspace.pk, "connection_id": connection.pk})
        with fake_bot_api() as fake:
            as_admin(client, tenancy).post(url)

        assert fake.methods() == ["deleteWebhook"]
        assert not ChannelConnection.objects.for_workspace(tenancy.workspace).exists()

    def test_a_platform_that_will_not_answer_does_not_block_the_disconnect(
        self, client: Client, tenancy: Tenancy
    ) -> None:
        """The operator asked to disconnect. Telegram being down is not a reason
        to refuse — the row goes and the failure is logged."""
        connection = ChannelConnection.objects.create(
            workspace=tenancy.workspace,
            platform=Platform.TELEGRAM,
            display_name="@acme_bot",
            external_id="777001",
            credentials={"bot_token": BOT_TOKEN},
        )
        url = reverse("channels:delete", kwargs={"workspace_id": tenancy.workspace.pk, "connection_id": connection.pk})
        with fake_bot_api(lambda fake: fake.reply("deleteWebhook", Reply(status=500))):
            as_admin(client, tenancy).post(url)
        assert not ChannelConnection.objects.for_workspace(tenancy.workspace).exists()


class TestWebhookHealth:
    def test_the_list_says_when_nothing_has_arrived(self, client: Client, tenancy: Tenancy) -> None:
        ChannelConnection.objects.create(
            workspace=tenancy.workspace,
            platform=Platform.TELEGRAM,
            display_name="@acme_bot",
            external_id="777002",
        )
        url = reverse("channels:list", kwargs={"workspace_id": tenancy.workspace.pk})
        assert "Nothing received yet" in as_admin(client, tenancy).get(url).content.decode()

    def test_the_detail_page_shows_the_last_event(self, client: Client, tenancy: Tenancy) -> None:
        connection = ChannelConnection.objects.create(
            workspace=tenancy.workspace,
            platform=Platform.TELEGRAM,
            display_name="@acme_bot",
            external_id="777003",
        )
        WebhookEventLog.objects.create(connection=connection, platform=Platform.TELEGRAM, provider_event_id="tg:1")
        url = reverse("channels:detail", kwargs={"workspace_id": tenancy.workspace.pk, "connection_id": connection.pk})
        body = as_admin(client, tenancy).get(url).content.decode()
        assert "Last event received" in body
        assert "Nothing yet" not in body

    def test_the_platform_list_links_to_the_guided_flow(self, client: Client, tenancy: Tenancy) -> None:
        url = reverse("channels:list", kwargs={"workspace_id": tenancy.workspace.pk})
        body = as_admin(client, tenancy).get(url).content.decode()
        assert connect_url(tenancy) in body
        # And no longer tells the operator to wait for the issue that shipped it.
        assert "#12 (L4-B)" not in body
