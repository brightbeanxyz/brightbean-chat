"""The guided connect flow and the template manager's pages (SPEC §6.5).

The ordering assertions are the point of most of this, and they are Telegram's:
verify before any write, because a credential that does not work should leave no
trace and because it is the only source of how the number displays; subscribe
last, because a number Meta will not deliver for is not a connection and must
not sit in the list looking like one.

The rest is the boundary work every settings page owes: admin-only, scoped
lookups, another tenant's ids answering 404, and a live credential that is never
rendered back.
"""

import re
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse

from apps.channels.models import ChannelConnection, WhatsAppTemplate, WhatsAppTemplateStatus
from apps.channels.providers import whatsapp
from apps.channels.tests.whatsapp_support import (
    ACCESS_TOKEN,
    PHONE_NUMBER_ID,
    WABA_ID,
    Reply,
    fake_graph_api,
    make_connection,
)
from apps.common.platforms import Platform
from apps.members.roles import WorkspaceRole
from tests.support import Tenancy

pytestmark = pytest.mark.django_db

NUMBER = {"id": PHONE_NUMBER_ID, "display_phone_number": "+1 555 000 1111", "verified_name": "Acme"}

FORM = {"waba_id": WABA_ID, "phone_number_id": PHONE_NUMBER_ID, "access_token": ACCESS_TOKEN}


def url(name: str, tenancy: Tenancy, **kwargs: Any) -> str:
    return reverse(f"channels:{name}", kwargs={"workspace_id": tenancy.workspace.pk, **kwargs})


def as_admin(client: Client, tenancy: Tenancy) -> Client:
    """``manage_channels`` is admin-only (apps.members.roles._ADMIN_ONLY_KEYS)."""
    client.force_login(tenancy.user_for(WorkspaceRole.ADMIN))
    return client


def verified(fake: Any) -> None:
    fake.reply(PHONE_NUMBER_ID, Reply(body=NUMBER))


class TestConnect:
    def test_good_credentials_create_a_connection_and_subscribe_the_app(self, client: Client, tenancy: Tenancy) -> None:
        with fake_graph_api(verified) as fake:
            response = as_admin(client, tenancy).post(url("whatsapp_connect", tenancy), FORM)

        assert response.status_code == 302
        connection = ChannelConnection.objects.for_workspace(tenancy.workspace).get()
        assert connection.platform == Platform.WHATSAPP
        assert connection.external_id == PHONE_NUMBER_ID
        # The display name comes from Meta, not from anything the operator typed.
        assert connection.display_name == "+1 555 000 1111"
        assert whatsapp.credentials_of(connection)["waba_id"] == WABA_ID
        assert f"/v21.0/{WABA_ID}/subscribed_apps" in fake.paths()

    def test_verification_comes_before_any_write(self, client: Client, tenancy: Tenancy) -> None:
        """A credential that does not work should leave no trace."""
        with fake_graph_api() as fake:
            fake.reply(PHONE_NUMBER_ID, Reply(status=401))
            response = as_admin(client, tenancy).post(url("whatsapp_connect", tenancy), FORM)

        assert response.status_code == 200
        assert "Meta did not accept" in response.content.decode()
        assert not ChannelConnection.objects.for_workspace(tenancy.workspace).exists()

    def test_a_failed_subscription_removes_the_connection_again(self, client: Client, tenancy: Tenancy) -> None:
        """A number Meta will not deliver for is not a connection, and one left
        in the list looking connected is the worse outcome."""
        with fake_graph_api() as fake:
            verified(fake)
            fake.reply("subscribed_apps", Reply(status=400))
            response = as_admin(client, tenancy).post(url("whatsapp_connect", tenancy), FORM)

        assert "could not be subscribed" in response.content.decode()
        assert not ChannelConnection.objects.for_workspace(tenancy.workspace).exists()

    def test_the_token_is_never_rendered_back(self, client: Client, tenancy: Tenancy) -> None:
        with fake_graph_api() as fake:
            fake.reply(PHONE_NUMBER_ID, Reply(status=401))
            response = as_admin(client, tenancy).post(url("whatsapp_connect", tenancy), FORM)
        assert ACCESS_TOKEN not in response.content.decode()

    def test_the_token_never_reaches_a_log(self, client: Client, tenancy: Tenancy, caplog: Any) -> None:
        import logging

        with caplog.at_level(logging.DEBUG), fake_graph_api() as fake:
            fake.reply(PHONE_NUMBER_ID, Reply(status=401))
            as_admin(client, tenancy).post(url("whatsapp_connect", tenancy), FORM)
        assert ACCESS_TOKEN not in caplog.text

    def test_the_rejection_message_is_the_same_for_every_reason(self, client: Client, tenancy: Tenancy) -> None:
        """Distinguishing them would be an oracle for whether an id or a token
        is real."""
        shown = set()
        for status in (400, 401, 403, 404):
            with fake_graph_api() as fake:
                fake.reply(PHONE_NUMBER_ID, Reply(status=status))
                body = as_admin(client, tenancy).post(url("whatsapp_connect", tenancy), FORM).content.decode()
            # The rendered alert, not the whole page: a CSP nonce and a CSRF
            # token differ per response and say nothing about the failure.
            found = re.findall(r'<div class="alert-error">.*?</div>', body, re.DOTALL)
            assert found, "the page should show an error"
            shown.update(found)
        assert len(shown) == 1

    def test_a_duplicate_number_is_refused_without_naming_the_other_workspace(
        self, client: Client, tenancy: Tenancy, other_tenancy: Tenancy
    ) -> None:
        """SPEC §5's unique (platform, external_id) is deployment-wide."""
        make_connection(other_tenancy.workspace)
        with fake_graph_api(verified):
            response = as_admin(client, tenancy).post(url("whatsapp_connect", tenancy), FORM)

        body = response.content.decode()
        assert "already connected to this deployment" in body
        assert other_tenancy.workspace.name not in body
        assert not ChannelConnection.objects.for_workspace(tenancy.workspace).exists()

    def test_a_non_numeric_id_is_refused_in_the_form(self, client: Client, tenancy: Tenancy) -> None:
        with fake_graph_api() as fake:
            response = as_admin(client, tenancy).post(
                url("whatsapp_connect", tenancy), {**FORM, "phone_number_id": "not-an-id"}
            )
        assert response.status_code == 200
        assert fake.calls == []

    def test_an_editor_may_not_connect_a_channel(self, client: Client, tenancy: Tenancy) -> None:
        client.force_login(tenancy.user_for(WorkspaceRole.EDITOR))
        assert client.get(url("whatsapp_connect", tenancy)).status_code == 403

    def test_the_generic_form_no_longer_offers_whatsapp(self, client: Client, tenancy: Tenancy) -> None:
        """A platform with a guided flow must not be reachable through the
        platform-agnostic form, which creates a row with no credentials."""
        response = as_admin(client, tenancy).post(
            reverse("channels:create", kwargs={"workspace_id": tenancy.workspace.pk}),
            {"platform": Platform.WHATSAPP, "display_name": "Sneaky", "external_id": "999"},
        )
        assert "guided setup" in response.content.decode()
        assert not ChannelConnection.objects.for_workspace(tenancy.workspace).exists()

    def test_disconnecting_unsubscribes_the_app(self, client: Client, tenancy: Tenancy) -> None:
        connection = make_connection(tenancy.workspace)
        with fake_graph_api() as fake:
            as_admin(client, tenancy).post(
                reverse(
                    "channels:delete",
                    kwargs={"workspace_id": tenancy.workspace.pk, "connection_id": connection.pk},
                )
            )
        assert any(path.endswith("subscribed_apps") for path in fake.paths())


class TestTemplatePages:
    @pytest.fixture
    def connection(self, tenancy: Tenancy) -> ChannelConnection:
        return make_connection(tenancy.workspace)

    def test_the_list_shows_a_templates_status_and_variables(
        self, client: Client, tenancy: Tenancy, connection: ChannelConnection
    ) -> None:
        WhatsAppTemplate(
            workspace=tenancy.workspace,
            channel_connection=connection,
            name="order_shipped",
            language="en_US",
            category="utility",
            body_structure={"body": {"text": "Hi {{1}}"}},
            status=WhatsAppTemplateStatus.REJECTED,
            rejected_reason="INVALID_FORMAT",
        ).save()

        body = as_admin(client, tenancy).get(url("whatsapp_templates", tenancy)).content.decode()
        assert "order_shipped" in body
        assert "INVALID_FORMAT" in body
        assert "body.1" in body

    def test_metas_reason_is_escaped_like_any_other_provider_string(
        self, client: Client, tenancy: Tenancy, connection: ChannelConnection
    ) -> None:
        WhatsAppTemplate(
            workspace=tenancy.workspace,
            channel_connection=connection,
            name="hostile",
            language="en_US",
            category="utility",
            body_structure={"body": {"text": "x"}},
            status=WhatsAppTemplateStatus.REJECTED,
            rejected_reason="<script>alert(1)</script>",
        ).save()

        body = as_admin(client, tenancy).get(url("whatsapp_templates", tenancy)).content.decode()
        assert "<script>alert(1)</script>" not in body
        assert "&lt;script&gt;" in body

    def test_the_new_template_page_renders(
        self, client: Client, tenancy: Tenancy, connection: ChannelConnection
    ) -> None:
        """The form page's own GET, which no other test reaches: the IDOR sweep
        hits it with another tenant's workspace id and is 404ed by the
        middleware before the template is ever rendered."""
        response = as_admin(client, tenancy).get(url("whatsapp_template_new", tenancy))
        assert response.status_code == 200
        body = response.content.decode()
        # The literal an operator has to type. Django's template language has no
        # escape for "{{", so the view passes the braces in as context.
        assert "{{1}}" in body
        assert connection.display_name in body

    def test_creating_a_template_stores_the_assembled_document(
        self, client: Client, tenancy: Tenancy, connection: ChannelConnection
    ) -> None:
        response = as_admin(client, tenancy).post(
            url("whatsapp_template_new", tenancy),
            {
                "channel_connection": str(connection.pk),
                "name": "order_shipped",
                "language": "en_US",
                "category": "utility",
                "header_text": "Order {{1}}",
                "body_text": "Hi {{1}}, order {{2}} shipped.",
                "footer_text": "Reply STOP to opt out.",
                "quick_reply_0": "Track",
                "url_button_text": "",
                "url_button_url": "",
            },
        )
        assert response.status_code == 302
        template = WhatsAppTemplate.objects.for_workspace(tenancy.workspace).get()
        assert template.status == WhatsAppTemplateStatus.DRAFT
        assert template.body_structure["body"]["text"].startswith("Hi {{1}}")
        assert template.body_structure["buttons"] == [{"type": "quick_reply", "text": "Track"}]

    def test_an_uppercase_name_is_refused_in_the_form(
        self, client: Client, tenancy: Tenancy, connection: ChannelConnection
    ) -> None:
        response = as_admin(client, tenancy).post(
            url("whatsapp_template_new", tenancy),
            {
                "channel_connection": str(connection.pk),
                "name": "Order Shipped",
                "language": "en_US",
                "category": "utility",
                "body_text": "Hi",
            },
        )
        assert response.status_code == 200
        assert not WhatsAppTemplate.objects.for_workspace(tenancy.workspace).exists()

    def test_another_tenants_connection_cannot_be_named(
        self, client: Client, tenancy: Tenancy, other_tenancy: Tenancy
    ) -> None:
        """The ModelChoiceField's queryset is scoped, so a hand-crafted POST is
        refused rather than filing the template against a stranger's number."""
        theirs = make_connection(other_tenancy.workspace)
        response = as_admin(client, tenancy).post(
            url("whatsapp_template_new", tenancy),
            {
                "channel_connection": str(theirs.pk),
                "name": "sneaky",
                "language": "en_US",
                "category": "utility",
                "body_text": "Hi",
            },
        )
        assert response.status_code == 200
        assert not WhatsAppTemplate.objects.unscoped().filter(name="sneaky").exists()

    def test_submitting_moves_it_to_pending(
        self, client: Client, tenancy: Tenancy, connection: ChannelConnection
    ) -> None:
        template = WhatsAppTemplate(
            workspace=tenancy.workspace,
            channel_connection=connection,
            name="order_shipped",
            language="en_US",
            category="utility",
            body_structure={"body": {"text": "Hi"}},
        )
        template.save()

        with fake_graph_api() as fake:
            fake.reply("message_templates", Reply(body={"id": "META_TPL_1"}))
            as_admin(client, tenancy).post(url("whatsapp_template_submit", tenancy, template_id=template.pk))

        template.refresh_from_db()
        assert template.status == WhatsAppTemplateStatus.PENDING

    def test_an_approved_template_is_not_editable(
        self, client: Client, tenancy: Tenancy, connection: ChannelConnection
    ) -> None:
        """The copy under review is the one Meta holds; editing the local row
        would make the two disagree with nothing to say which is live."""
        template = WhatsAppTemplate(
            workspace=tenancy.workspace,
            channel_connection=connection,
            name="live_one",
            language="en_US",
            category="utility",
            body_structure={"body": {"text": "Original"}},
            status=WhatsAppTemplateStatus.APPROVED,
        )
        template.save()

        as_admin(client, tenancy).post(
            url("whatsapp_template_edit", tenancy, template_id=template.pk),
            {
                "channel_connection": str(connection.pk),
                "name": "live_one",
                "language": "en_US",
                "category": "utility",
                "body_text": "Changed",
            },
        )
        template.refresh_from_db()
        assert template.body_structure["body"]["text"] == "Original"

    @pytest.mark.parametrize(
        "route", ["whatsapp_template_edit", "whatsapp_template_submit", "whatsapp_template_delete"]
    )
    def test_another_tenants_template_is_a_404(
        self, client: Client, tenancy: Tenancy, other_tenancy: Tenancy, route: str
    ) -> None:
        """404 rather than 403: a 403 would confirm the id names something real."""
        theirs = WhatsAppTemplate(
            workspace=other_tenancy.workspace,
            channel_connection=make_connection(other_tenancy.workspace),
            name="theirs",
            language="en_US",
            category="utility",
            body_structure={"body": {"text": "x"}},
        )
        theirs.save()

        response = as_admin(client, tenancy).post(url(route, tenancy, template_id=theirs.pk))
        assert response.status_code == 404

    def test_an_editor_may_not_reach_the_templates(self, client: Client, tenancy: Tenancy) -> None:
        client.force_login(tenancy.user_for(WorkspaceRole.EDITOR))
        assert client.get(url("whatsapp_templates", tenancy)).status_code == 403


class TestPreviewFragment:
    def test_it_renders_what_is_currently_typed(self, client: Client, tenancy: Tenancy) -> None:
        response = as_admin(client, tenancy).post(
            url("whatsapp_template_preview", tenancy),
            {"body_text": "Hi {{1}}", "sample.body.1": "Ada"},
        )
        assert "Hi Ada" in response.content.decode()

    def test_a_sample_value_is_never_evaluated(self, client: Client, tenancy: Tenancy) -> None:
        """SECURITY-BASELINE §3, through the same renderer the send path uses."""
        response = as_admin(client, tenancy).post(
            url("whatsapp_template_preview", tenancy),
            {"body_text": "Hi {{1}}", "sample.body.1": "{{2}}"},
        )
        assert "{{2}}" in response.content.decode()

    def test_a_sample_value_is_escaped_on_render(self, client: Client, tenancy: Tenancy) -> None:
        response = as_admin(client, tenancy).post(
            url("whatsapp_template_preview", tenancy),
            {"body_text": "Hi {{1}}", "sample.body.1": "<script>alert(1)</script>"},
        )
        body = response.content.decode()
        assert "<script>alert(1)</script>" not in body
        assert "&lt;script&gt;" in body

    def test_it_is_post_only(self, client: Client, tenancy: Tenancy) -> None:
        assert as_admin(client, tenancy).get(url("whatsapp_template_preview", tenancy)).status_code == 405


class TestCostHintPage:
    def test_saving_estimates_round_trips(self, client: Client, tenancy: Tenancy) -> None:
        response = as_admin(client, tenancy).post(
            url("whatsapp_cost_hints", tenancy),
            {"currency": "eur", "marketing": "0.0512", "utility": "0.01", "authentication": "0.02"},
        )
        assert response.status_code == 302

        body = as_admin(client, tenancy).get(url("whatsapp_cost_hints", tenancy)).content.decode()
        assert "EUR" in body

    def test_a_negative_price_is_refused(self, client: Client, tenancy: Tenancy) -> None:
        from apps.channels.models import WhatsAppCostHint

        response = as_admin(client, tenancy).post(
            url("whatsapp_cost_hints", tenancy),
            {"currency": "USD", "marketing": "-1", "utility": "0", "authentication": "0"},
        )
        assert response.status_code == 200
        assert not WhatsAppCostHint.objects.for_workspace(tenancy.workspace).exists()
