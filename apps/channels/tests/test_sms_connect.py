"""The Twilio connect flow, the SMS settings page, and the segment preview.

Two properties carry most of the weight here, and both are about what an
operator is *not* told. The connect flow validates against Twilio before it
writes anything, so a wrong credential leaves no row; and it answers one message
for every reason the credentials can be rejected, so the page is not an oracle
for whether a given account SID exists.

The webhook URL is the third: what this page shows and what
``verify_webhook`` recomputes come from one function, because a settings page
that disagreed with the verifier by one character would reject every genuine
delivery with nothing to say why.
"""

from typing import Any

import pytest
from django.test import Client
from django.urls import reverse

from apps.channels.models import ChannelConnection, SmsSettings
from apps.channels.providers.sms import account_sid, auth_token, sender_params, webhook_url
from apps.channels.tests.sms_support import (
    ACCOUNT_SID,
    AUTH_TOKEN,
    FROM_NUMBER,
    MESSAGING_SERVICE_SID,
    FakeTwilio,
    Reply,
    fake_twilio,
    sms_connection,
)
from apps.common.platforms import Platform
from apps.members.roles import WorkspaceRole
from tests.support import Tenancy

pytestmark = pytest.mark.django_db

ACCOUNT_OK = Reply({"sid": ACCOUNT_SID, "friendly_name": "Acme", "status": "active"})
NUMBER_OK = Reply({"incoming_phone_numbers": [{"sid": "PN1", "phone_number": FROM_NUMBER}]})
SERVICE_OK = Reply({"sid": MESSAGING_SERVICE_SID, "friendly_name": "Acme campaign"})


def url_for(name: str, tenancy: Tenancy) -> str:
    return reverse(f"channels:{name}", kwargs={"workspace_id": tenancy.workspace.pk})


def as_admin(client: Client, tenancy: Tenancy) -> Client:
    """``manage_channels`` is admin-only (apps.members.roles._ADMIN_ONLY_KEYS)."""
    client.force_login(tenancy.user_for(WorkspaceRole.ADMIN))
    return client


def happy_twilio() -> FakeTwilio:
    fake = FakeTwilio()
    fake.reply(f"{ACCOUNT_SID}.json", ACCOUNT_OK)
    fake.reply("IncomingPhoneNumbers.json", NUMBER_OK)
    fake.reply(MESSAGING_SERVICE_SID, SERVICE_OK)
    return fake


class TestConnect:
    def test_good_credentials_create_a_connection(self, client: Client, tenancy: Tenancy) -> None:
        with fake_twilio(happy_twilio()):
            response = as_admin(client, tenancy).post(
                url_for("sms_connect", tenancy),
                {"account_sid": ACCOUNT_SID, "auth_token": AUTH_TOKEN, "from_number": FROM_NUMBER},
            )

        assert response.status_code == 302
        connection = ChannelConnection.objects.for_workspace(tenancy.workspace).get()
        assert connection.platform == Platform.SMS
        assert connection.external_id == FROM_NUMBER
        assert account_sid(connection) == ACCOUNT_SID
        assert auth_token(connection) == AUTH_TOKEN
        assert sender_params(connection) == {"From": FROM_NUMBER}

    def test_a_messaging_service_connects_too(self, client: Client, tenancy: Tenancy) -> None:
        with fake_twilio(happy_twilio()):
            as_admin(client, tenancy).post(
                url_for("sms_connect", tenancy),
                {
                    "account_sid": ACCOUNT_SID,
                    "auth_token": AUTH_TOKEN,
                    "messaging_service_sid": MESSAGING_SERVICE_SID,
                },
            )

        connection = ChannelConnection.objects.for_workspace(tenancy.workspace).get()
        assert sender_params(connection) == {"MessagingServiceSid": MESSAGING_SERVICE_SID}

    def test_the_account_is_checked_before_anything_is_written(self, client: Client, tenancy: Tenancy) -> None:
        fake = happy_twilio()
        fake.reply(f"{ACCOUNT_SID}.json", Reply({"code": 20003}, status=401))

        with fake_twilio(fake):
            response = as_admin(client, tenancy).post(
                url_for("sms_connect", tenancy),
                {"account_sid": ACCOUNT_SID, "auth_token": "wrong", "from_number": FROM_NUMBER},
            )

        assert response.status_code == 200
        assert not ChannelConnection.objects.for_workspace(tenancy.workspace).exists()
        # And it stopped there: the number was never looked up with a token that
        # does not work.
        assert fake.forms("IncomingPhoneNumbers.json") == []

    def test_a_number_the_account_does_not_hold_is_refused(self, client: Client, tenancy: Tenancy) -> None:
        """Otherwise the connection fails on its first send, days later, in production."""
        fake = happy_twilio()
        fake.reply("IncomingPhoneNumbers.json", Reply({"incoming_phone_numbers": []}))

        with fake_twilio(fake):
            response = as_admin(client, tenancy).post(
                url_for("sms_connect", tenancy),
                {"account_sid": ACCOUNT_SID, "auth_token": AUTH_TOKEN, "from_number": "+15559999999"},
            )

        assert "does not hold that number" in response.content.decode()
        assert not ChannelConnection.objects.for_workspace(tenancy.workspace).exists()

    @pytest.mark.parametrize(
        "payload",
        [
            {"account_sid": "", "auth_token": AUTH_TOKEN, "from_number": FROM_NUMBER},
            {"account_sid": ACCOUNT_SID, "auth_token": "", "from_number": FROM_NUMBER},
        ],
    )
    def test_missing_credentials_are_refused_without_calling_twilio(
        self, client: Client, tenancy: Tenancy, payload: dict[str, str]
    ) -> None:
        with fake_twilio() as fake:
            as_admin(client, tenancy).post(url_for("sms_connect", tenancy), payload)

        assert fake.calls == []
        assert not ChannelConnection.objects.for_workspace(tenancy.workspace).exists()

    def test_both_a_number_and_a_service_is_refused(self, client: Client, tenancy: Tenancy) -> None:
        """Twilio's Messages API accepts one or the other and rejects both, so a
        row holding both would be a connection whose every send fails."""
        with fake_twilio(happy_twilio()):
            response = as_admin(client, tenancy).post(
                url_for("sms_connect", tenancy),
                {
                    "account_sid": ACCOUNT_SID,
                    "auth_token": AUTH_TOKEN,
                    "from_number": FROM_NUMBER,
                    "messaging_service_sid": MESSAGING_SERVICE_SID,
                },
            )

        assert "not both, and not neither" in response.content.decode()
        assert not ChannelConnection.objects.for_workspace(tenancy.workspace).exists()

    def test_neither_is_refused_too(self, client: Client, tenancy: Tenancy) -> None:
        with fake_twilio(happy_twilio()):
            response = as_admin(client, tenancy).post(
                url_for("sms_connect", tenancy),
                {"account_sid": ACCOUNT_SID, "auth_token": AUTH_TOKEN},
            )

        assert "not both, and not neither" in response.content.decode()

    def test_a_number_already_connected_says_so_without_naming_the_workspace(
        self, client: Client, tenancy: Tenancy, other_tenancy: Tenancy
    ) -> None:
        """SPEC §5's unique (platform, external_id) is deployment-wide, so this
        can be another tenant's row (SECURITY-BASELINE §1)."""
        sms_connection(other_tenancy.workspace)

        with fake_twilio(happy_twilio()):
            response = as_admin(client, tenancy).post(
                url_for("sms_connect", tenancy),
                {"account_sid": ACCOUNT_SID, "auth_token": AUTH_TOKEN, "from_number": FROM_NUMBER},
            )

        body = response.content.decode()
        assert "already connected to this deployment" in body
        assert other_tenancy.workspace.name not in body

    def test_the_page_never_echoes_the_token_back(self, client: Client, tenancy: Tenancy) -> None:
        fake = happy_twilio()
        fake.reply(f"{ACCOUNT_SID}.json", Reply({"code": 20003}, status=401))

        with fake_twilio(fake):
            response = as_admin(client, tenancy).post(
                url_for("sms_connect", tenancy),
                {"account_sid": ACCOUNT_SID, "auth_token": AUTH_TOKEN, "from_number": FROM_NUMBER},
            )

        assert AUTH_TOKEN not in response.content.decode()

    def test_the_token_never_reaches_a_log(self, client: Client, tenancy: Tenancy, caplog: Any) -> None:
        fake = happy_twilio()
        fake.reply(f"{ACCOUNT_SID}.json", Reply({"code": 20003}, status=401))

        with caplog.at_level("DEBUG"), fake_twilio(fake):
            as_admin(client, tenancy).post(
                url_for("sms_connect", tenancy),
                {"account_sid": ACCOUNT_SID, "auth_token": AUTH_TOKEN, "from_number": FROM_NUMBER},
            )

        assert AUTH_TOKEN not in caplog.text

    @pytest.mark.parametrize("role", [WorkspaceRole.EDITOR, WorkspaceRole.AGENT, WorkspaceRole.VIEWER])
    def test_only_manage_channels_may_connect(self, client: Client, tenancy: Tenancy, role: str) -> None:
        client.force_login(tenancy.user_for(role))

        assert client.get(url_for("sms_connect", tenancy)).status_code == 403

    def test_another_tenants_workspace_is_404(self, client: Client, tenancy: Tenancy, other_tenancy: Tenancy) -> None:
        client.force_login(other_tenancy.user_for(WorkspaceRole.ADMIN))

        assert client.get(url_for("sms_connect", tenancy)).status_code == 404


class TestWebhookUrl:
    def test_the_settings_page_shows_the_url_the_adapter_verifies_against(
        self, client: Client, tenancy: Tenancy
    ) -> None:
        """One builder, three consumers. A page that showed
        ``build_absolute_uri`` while the adapter verified ``APP_URL`` would hand
        an operator a URL whose every delivery is then rejected."""
        connection = sms_connection(tenancy.workspace)
        detail = reverse(
            "channels:detail",
            kwargs={"workspace_id": tenancy.workspace.pk, "connection_id": connection.pk},
        )

        response = as_admin(client, tenancy).get(detail)

        assert webhook_url(connection) in response.content.decode()

    def test_the_detail_page_links_to_the_sms_settings(self, client: Client, tenancy: Tenancy) -> None:
        """Otherwise the settings route is an orphan — reachable only by typing
        it, which is how a compliance page ends up never being read."""
        connection = sms_connection(tenancy.workspace)
        detail = reverse(
            "channels:detail",
            kwargs={"workspace_id": tenancy.workspace.pk, "connection_id": connection.pk},
        )

        body = as_admin(client, tenancy).get(detail).content.decode()

        assert url_for("sms_settings", tenancy) in body

    def test_a_platform_without_a_settings_page_gets_no_link(self, client: Client, tenancy: Tenancy) -> None:
        connection = ChannelConnection.objects.create(
            workspace=tenancy.workspace,
            platform=Platform.TELEGRAM.value,
            display_name="@acme_bot",
            external_id="777000",
        )
        detail = reverse(
            "channels:detail",
            kwargs={"workspace_id": tenancy.workspace.pk, "connection_id": connection.pk},
        )

        assert "settings for this workspace" not in as_admin(client, tenancy).get(detail).content.decode()

    def test_it_is_built_from_app_url_not_the_request(self, tenancy: Tenancy) -> None:
        from django.conf import settings

        connection = sms_connection(tenancy.workspace)

        assert webhook_url(connection).startswith(settings.APP_URL.rstrip("/"))
        assert str(connection.pk) in webhook_url(connection)


class TestSettings:
    def test_the_page_renders_before_anything_is_saved(self, client: Client, tenancy: Tenancy) -> None:
        """A GET must not write. The defaults come from properties on an unsaved
        instance, so there is nothing to create."""
        response = as_admin(client, tenancy).get(url_for("sms_settings", tenancy))

        assert response.status_code == 200
        assert not SmsSettings.objects.for_workspace(tenancy.workspace).exists()

    def test_it_lists_the_hard_coded_keywords(self, client: Client, tenancy: Tenancy) -> None:
        body = as_admin(client, tenancy).get(url_for("sms_settings", tenancy)).content.decode()

        for word in ("STOP", "UNSUBSCRIBE", "HELP", "START", "UNSTOP"):
            assert word in body

    def test_saving_stores_the_copy(self, client: Client, tenancy: Tenancy) -> None:
        as_admin(client, tenancy).post(
            url_for("sms_settings_update", tenancy),
            {
                "help_text_body": "Acme support: 555-0000.",
                "opt_out_confirmation": "Unsubscribed.",
                "opt_in_confirmation": "Welcome back.",
                "per_segment_cost": "0.0079",
                "a2p_brand_registered": "on",
            },
        )

        row = SmsSettings.objects.for_workspace(tenancy.workspace).get()
        assert row.help_reply == "Acme support: 555-0000."
        assert row.opt_out_reply == "Unsubscribed."
        assert row.opt_in_reply == "Welcome back."
        assert str(row.per_segment_cost) == "0.00790"
        assert row.a2p_brand_registered is True
        assert row.a2p_campaign_approved is False

    def test_blanking_a_reply_restores_the_default_rather_than_silence(self, client: Client, tenancy: Tenancy) -> None:
        """These replies are legally required; "off" is not an option the page offers."""
        from apps.channels.models import DEFAULT_HELP_TEXT

        SmsSettings.objects.create(workspace=tenancy.workspace, help_text_body="Old wording")

        as_admin(client, tenancy).post(url_for("sms_settings_update", tenancy), {"help_text_body": "   "})

        assert SmsSettings.objects.for_workspace(tenancy.workspace).get().help_reply == DEFAULT_HELP_TEXT

    @pytest.mark.parametrize("value", ["", "  ", "free", "-1", "1e999", "10000"])
    def test_an_unusable_price_is_stored_as_not_set(self, client: Client, tenancy: Tenancy, value: str) -> None:
        """None rather than zero: a blank field and a typo both mean "we do not
        know what this costs", and 0.00 would be a confident wrong answer."""
        as_admin(client, tenancy).post(url_for("sms_settings_update", tenancy), {"per_segment_cost": value})

        assert SmsSettings.objects.for_workspace(tenancy.workspace).get().per_segment_cost is None

    def test_a_long_reply_is_bounded(self, client: Client, tenancy: Tenancy) -> None:
        as_admin(client, tenancy).post(url_for("sms_settings_update", tenancy), {"help_text_body": "x" * 5000})

        assert len(SmsSettings.objects.for_workspace(tenancy.workspace).get().help_text_body) == 1600

    @pytest.mark.parametrize("role", [WorkspaceRole.EDITOR, WorkspaceRole.AGENT, WorkspaceRole.VIEWER])
    def test_only_manage_channels_may_read_or_write_settings(self, client: Client, tenancy: Tenancy, role: str) -> None:
        client.force_login(tenancy.user_for(role))

        assert client.get(url_for("sms_settings", tenancy)).status_code == 403
        assert client.post(url_for("sms_settings_update", tenancy), {}).status_code == 403

    def test_another_tenants_settings_are_404(self, client: Client, tenancy: Tenancy, other_tenancy: Tenancy) -> None:
        client.force_login(other_tenancy.user_for(WorkspaceRole.ADMIN))

        assert client.get(url_for("sms_settings", tenancy)).status_code == 404
        assert client.post(url_for("sms_settings_update", tenancy), {}).status_code == 404

    def test_settings_are_scoped_per_workspace(self, tenancy: Tenancy, other_tenancy: Tenancy) -> None:
        SmsSettings.objects.create(workspace=tenancy.workspace, help_text_body="Ours")
        SmsSettings.objects.create(workspace=other_tenancy.workspace, help_text_body="Theirs")

        assert SmsSettings.objects.for_workspace(tenancy.workspace).get().help_text_body == "Ours"


class TestSegmentPreview:
    def test_it_renders_the_count(self, client: Client, tenancy: Tenancy) -> None:
        response = as_admin(client, tenancy).post(url_for("sms_segment_preview", tenancy), {"text": "a" * 161})

        body = response.content.decode()
        assert "GSM-7" in body
        assert "2 segments" in body

    def test_it_warns_about_a_ucs2_downgrade(self, client: Client, tenancy: Tenancy) -> None:
        body = (
            as_admin(client, tenancy)
            .post(url_for("sms_segment_preview", tenancy), {"text": "It’s here"})
            .content.decode()
        )

        assert "UCS-2" in body
        assert "70" in body

    def test_it_estimates_a_cost_when_one_is_configured(self, client: Client, tenancy: Tenancy) -> None:
        SmsSettings.objects.create(workspace=tenancy.workspace, per_segment_cost="0.0100")

        body = (
            as_admin(client, tenancy)
            .post(url_for("sms_segment_preview", tenancy), {"text": "a" * 161})
            .content.decode()
        )

        assert "0.02" in body

    def test_it_says_nothing_about_cost_when_none_is_configured(self, client: Client, tenancy: Tenancy) -> None:
        body = as_admin(client, tenancy).post(url_for("sms_segment_preview", tenancy), {"text": "hi"}).content.decode()

        assert "per recipient" not in body

    def test_it_is_post_only(self, client: Client, tenancy: Tenancy) -> None:
        """Draft message text must not land in a URL, an access log or a history."""
        assert as_admin(client, tenancy).get(url_for("sms_segment_preview", tenancy)).status_code == 405

    def test_an_editor_may_use_it(self, client: Client, tenancy: Tenancy) -> None:
        """Its callers are the send_sms panel and L6-B's composer — a flow author
        who may well not administer channels."""
        client.force_login(tenancy.user_for(WorkspaceRole.EDITOR))

        assert client.post(url_for("sms_segment_preview", tenancy), {"text": "hi"}).status_code == 200

    @pytest.mark.parametrize("role", [WorkspaceRole.AGENT, WorkspaceRole.VIEWER])
    def test_a_role_without_edit_flows_may_not(self, client: Client, tenancy: Tenancy, role: str) -> None:
        client.force_login(tenancy.user_for(role))

        assert client.post(url_for("sms_segment_preview", tenancy), {"text": "hi"}).status_code == 403

    def test_a_hostile_body_is_escaped_not_executed(self, client: Client, tenancy: Tenancy) -> None:
        body = (
            as_admin(client, tenancy)
            .post(url_for("sms_segment_preview", tenancy), {"text": "<script>alert(1)</script>"})
            .content.decode()
        )

        assert "<script>" not in body

    def test_an_enormous_body_is_bounded(self, client: Client, tenancy: Tenancy) -> None:
        response = as_admin(client, tenancy).post(url_for("sms_segment_preview", tenancy), {"text": "a" * 50_000})

        assert response.status_code == 200
        assert "4000 characters" in response.content.decode()


class TestConnectRoute:
    def test_the_generic_form_refuses_sms_now_that_it_has_a_guided_flow(self, client: Client, tenancy: Tenancy) -> None:
        """A row with no credentials is an active-looking channel whose every
        send fails, which is what ``CONNECT_ROUTES`` exists to prevent."""
        from apps.channels.registry import connect_route_for

        assert connect_route_for(Platform.SMS.value) == "channels:sms_connect"

        response = as_admin(client, tenancy).post(
            url_for("create", tenancy),
            {"platform": Platform.SMS.value, "display_name": "Sneaky", "external_id": "+15550000000"},
        )

        assert "guided setup" in response.content.decode()
        assert not ChannelConnection.objects.for_workspace(tenancy.workspace).exists()

    def test_the_channel_list_links_to_it(self, client: Client, tenancy: Tenancy) -> None:
        body = as_admin(client, tenancy).get(url_for("list", tenancy)).content.decode()

        assert url_for("sms_connect", tenancy) in body
