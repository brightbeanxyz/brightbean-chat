"""The guided connect flow and the "send test email" action.

The ordering under test is ``views_telegram._connect``'s, and it is the part
worth asserting rather than the form rendering: **credentials are verified
before anything is written**, so a mistyped password leaves no row behind, and
the page never echoes a secret back.
"""

from typing import Any

import pytest
from django.test import Client

from apps.channels.models import ChannelConnection
from apps.channels.providers import email_backends
from apps.channels.providers.exceptions import APIError
from apps.channels.tests.email_support import DummySMTPServer, FakeSESClient
from apps.common.platforms import Platform

pytestmark = pytest.mark.django_db

SMTP_PASSWORD = "hunter2-not-a-real-password"
RESEND_KEY = "re_TestKeyAbcdefghijklmnop"
SES_SECRET = "wJalrXUtnFEMI-K7MDENG-bPxRfiCY-EXAMPLEKEY"


@pytest.fixture
def admin_client(tenancy: Any, client_for: Any) -> Client:
    return client_for(tenancy.user_for("admin"))


def connect_url(tenancy: Any) -> str:
    return f"/w/{tenancy.workspace.pk}/settings/channels/email/connect/"


def connections(tenancy: Any) -> Any:
    return ChannelConnection.objects.for_workspace(tenancy.workspace).filter(platform=Platform.EMAIL.value)


class TestSMTPConnect:
    def test_a_working_server_creates_a_connection(self, admin_client: Client, tenancy: Any) -> None:
        with DummySMTPServer() as server:
            response = admin_client.post(
                connect_url(tenancy),
                {
                    "provider": "smtp",
                    "from_address": "Hello@Sender.TEST",
                    "from_name": "Sender",
                    "host": "127.0.0.1",
                    "port": str(server.port),
                    "security": "none",
                    "username": "postmaster",
                    "password": SMTP_PASSWORD,
                },
            )

        assert response.status_code == 302
        connection = connections(tenancy).get()
        # SPEC §5: the sending domain, lowercased by normalize_email.
        assert connection.external_id == "sender.test"
        assert connection.display_name == "sender.test"
        assert connection.credentials["provider"] == "smtp"
        assert connection.credentials["from_address"] == "hello@sender.test"

    def test_an_unreachable_server_writes_nothing(self, admin_client: Client, tenancy: Any) -> None:
        """The whole point of verifying first: a bad credential leaves no trace."""
        response = admin_client.post(
            connect_url(tenancy),
            {
                "provider": "smtp",
                "from_address": "hello@sender.test",
                "host": "127.0.0.1",
                "port": "1",
                "security": "none",
                "password": SMTP_PASSWORD,
            },
        )

        assert response.status_code == 200
        assert connections(tenancy).count() == 0
        assert b"did not accept those details" in response.content

    def test_the_password_is_never_echoed_back(self, admin_client: Client, tenancy: Any) -> None:
        response = admin_client.post(
            connect_url(tenancy),
            {
                "provider": "smtp",
                "from_address": "hello@sender.test",
                "host": "127.0.0.1",
                "port": "1",
                "security": "none",
                "username": "postmaster",
                "password": SMTP_PASSWORD,
            },
        )
        assert SMTP_PASSWORD.encode() not in response.content
        # The non-secret fields are echoed, so a rejected submit is not retyped.
        assert b"postmaster" in response.content

    @pytest.mark.parametrize(
        ("payload", "message"),
        [
            ({"from_address": ""}, b"address this channel should send from"),
            ({"from_address": "not an address"}, b"address this channel should send from"),
            ({"host": ""}, b"SMTP host"),
            ({"port": "not a number"}, b"port is a number"),
            ({"security": "carrier-pigeon"}, b"STARTTLS, SSL or none"),
        ],
    )
    def test_a_bad_field_is_reported_without_touching_the_network(
        self, admin_client: Client, tenancy: Any, payload: dict[str, str], message: bytes
    ) -> None:
        base = {
            "provider": "smtp",
            "from_address": "hello@sender.test",
            "host": "mail.test",
            "port": "587",
            "security": "starttls",
        }
        response = admin_client.post(connect_url(tenancy), {**base, **payload})
        assert response.status_code == 200
        assert message in response.content
        assert connections(tenancy).count() == 0

    def test_a_second_workspace_cannot_claim_the_same_domain(
        self, admin_client: Client, tenancy: Any, other_tenancy: Any
    ) -> None:
        """SPF, DKIM and DMARC are properties of a domain, so it has one owner."""
        ChannelConnection.objects.create(
            workspace=other_tenancy.workspace,
            platform=Platform.EMAIL.value,
            display_name="Theirs",
            external_id="sender.test",
        )
        with DummySMTPServer() as server:
            response = admin_client.post(
                connect_url(tenancy),
                {
                    "provider": "smtp",
                    "from_address": "hello@sender.test",
                    "host": "127.0.0.1",
                    "port": str(server.port),
                    "security": "none",
                },
            )
        assert response.status_code == 200
        assert connections(tenancy).count() == 0


class TestResendConnect:
    def test_a_good_key_creates_a_connection(
        self, admin_client: Client, tenancy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(email_backends, "verify_credentials", lambda connection: None)
        response = admin_client.post(
            connect_url(tenancy),
            {
                "provider": "resend",
                "from_address": "hello@sender.test",
                "api_key": RESEND_KEY,
                "signing_secret": "whsec_abc",
            },
        )
        assert response.status_code == 302
        connection = connections(tenancy).get()
        assert connection.credentials["provider"] == "resend"
        assert connection.credentials["api_key"] == RESEND_KEY

    def test_a_rejected_key_writes_nothing_and_is_not_echoed(
        self, admin_client: Client, tenancy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def refuse(connection: Any) -> None:
            raise APIError("Resend said no", status_code=401)

        monkeypatch.setattr(email_backends, "verify_credentials", refuse)
        response = admin_client.post(
            connect_url(tenancy),
            {"provider": "resend", "from_address": "hello@sender.test", "api_key": RESEND_KEY},
        )
        assert connections(tenancy).count() == 0
        assert RESEND_KEY.encode() not in response.content
        # The provider's own text is not shown: it quotes the request, which on
        # an auth path can quote the credential.
        assert b"Resend said no" not in response.content

    def test_no_key_is_refused(self, admin_client: Client, tenancy: Any) -> None:
        response = admin_client.post(connect_url(tenancy), {"provider": "resend", "from_address": "hello@sender.test"})
        assert b"API key" in response.content
        assert connections(tenancy).count() == 0

    def test_the_signing_secret_is_optional(
        self, admin_client: Client, tenancy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Bounce handling needs it; sending does not, so it does not block connecting."""
        monkeypatch.setattr(email_backends, "verify_credentials", lambda connection: None)
        response = admin_client.post(
            connect_url(tenancy),
            {"provider": "resend", "from_address": "hello@sender.test", "api_key": RESEND_KEY},
        )
        assert response.status_code == 302
        assert connections(tenancy).get().credentials["signing_secret"] == ""


class TestSESConnect:
    def test_a_good_key_pair_creates_a_connection(
        self, admin_client: Client, tenancy: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(email_backends, "ses_client", lambda *a, **k: FakeSESClient())
        response = admin_client.post(
            connect_url(tenancy),
            {
                "provider": "ses",
                "from_address": "hello@sender.test",
                "access_key_id": "AKIA0000000000000000",
                "secret_access_key": SES_SECRET,
                "region": "EU-WEST-1",
            },
        )
        assert response.status_code == 302
        connection = connections(tenancy).get()
        assert connection.credentials["region"] == "eu-west-1"
        assert connection.credentials["secret_access_key"] == SES_SECRET

    def test_the_secret_is_never_echoed_back(self, admin_client: Client, tenancy: Any) -> None:
        response = admin_client.post(
            connect_url(tenancy),
            {
                "provider": "ses",
                "from_address": "hello@sender.test",
                "access_key_id": "AKIA0000000000000000",
                "secret_access_key": SES_SECRET,
                "region": "",
            },
        )
        assert SES_SECRET.encode() not in response.content
        assert connections(tenancy).count() == 0


class TestWebhookUrl:
    def test_the_detail_page_shows_the_providers_own_segment(self, admin_client: Client, tenancy: Any) -> None:
        """The segment tells the adapter which body shape to expect (SPEC §6.7)."""
        connection = ChannelConnection(
            workspace=tenancy.workspace,
            platform=Platform.EMAIL.value,
            display_name="Sender",
            external_id="sender.test",
        )
        connection.credentials = {"provider": "resend", "api_key": "k"}  # type: ignore[assignment]
        connection.save()

        response = admin_client.get(f"/w/{tenancy.workspace.pk}/settings/channels/{connection.pk}/")
        assert f"/webhooks/email/resend/{connection.pk}/".encode() in response.content


class TestSendTestEmail:
    def _connection(self, tenancy: Any, credentials: dict[str, Any]) -> ChannelConnection:
        connection = ChannelConnection(
            workspace=tenancy.workspace,
            platform=Platform.EMAIL.value,
            display_name="Sender",
            external_id="sender.test",
        )
        connection.credentials = credentials  # type: ignore[assignment]
        connection.save()
        return connection

    def test_it_sends_to_the_signed_in_members_own_address(self, admin_client: Client, tenancy: Any) -> None:
        """Never an address from the request: that would be an open relay."""
        with DummySMTPServer() as server:
            connection = self._connection(tenancy, server.credentials())
            response = admin_client.post(f"/w/{tenancy.workspace.pk}/settings/channels/{connection.pk}/test-email/")

        assert response.status_code == 200
        assert response.json()["ok"] is True
        import email as email_module

        parsed = email_module.message_from_string(server.messages[0])
        assert parsed["To"] == tenancy.user_for("admin").email

    def test_the_test_carries_the_compliance_headers(self, admin_client: Client, tenancy: Any) -> None:
        """So an operator whose provider strips them finds out here, not later."""
        with DummySMTPServer() as server:
            connection = self._connection(tenancy, server.credentials())
            admin_client.post(f"/w/{tenancy.workspace.pk}/settings/channels/{connection.pk}/test-email/")

        import email as email_module

        parsed = email_module.message_from_string(server.messages[0])
        # Unfolded before comparing: a long URL makes the header wrap across
        # lines, which is ordinary RFC 5322 folding and what a client undoes.
        unsubscribe = " ".join(parsed["List-Unsubscribe"].split())
        assert unsubscribe.startswith("<http") and unsubscribe.endswith("/>")
        assert parsed["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"

    def test_the_tests_unsubscribe_link_resolves_to_a_404(
        self, admin_client: Client, tenancy: Any, client: Client
    ) -> None:
        """Well-formed so providers accept the header; pointing at nothing.

        A test send has no identity, and minting one would create a consent
        record for a message nobody consented to.
        """
        with DummySMTPServer() as server:
            connection = self._connection(tenancy, server.credentials())
            admin_client.post(f"/w/{tenancy.workspace.pk}/settings/channels/{connection.pk}/test-email/")

        import email as email_module

        parsed = email_module.message_from_string(server.messages[0])
        path = " ".join(parsed["List-Unsubscribe"].split()).strip("<>").split("/u/", 1)[1]
        assert client.get(f"/u/{path}").status_code == 404

    def test_a_failure_is_a_200_with_ok_false(self, admin_client: Client, tenancy: Any) -> None:
        """The request succeeded; what it reports is somebody's mail configuration."""
        connection = self._connection(
            tenancy,
            {
                "provider": "smtp",
                "host": "127.0.0.1",
                "port": 1,
                "security": "none",
                "from_address": "hello@sender.test",
            },
        )
        response = admin_client.post(f"/w/{tenancy.workspace.pk}/settings/channels/{connection.pk}/test-email/")
        assert response.status_code == 200
        assert response.json()["ok"] is False

    def test_another_workspace_gets_a_404(self, tenancy: Any, other_tenancy: Any, client_for: Any) -> None:
        connection = self._connection(tenancy, {"provider": "smtp", "host": "mail.test"})
        attacker = client_for(other_tenancy.user_for("admin"))
        response = attacker.post(f"/w/{other_tenancy.workspace.pk}/settings/channels/{connection.pk}/test-email/")
        assert response.status_code == 404

    def test_a_get_is_refused(self, admin_client: Client, tenancy: Any) -> None:
        connection = self._connection(tenancy, {"provider": "smtp", "host": "mail.test"})
        response = admin_client.get(f"/w/{tenancy.workspace.pk}/settings/channels/{connection.pk}/test-email/")
        assert response.status_code == 405

    def test_a_viewer_cannot_send_one(self, tenancy: Any, client_for: Any) -> None:
        connection = self._connection(tenancy, {"provider": "smtp", "host": "mail.test"})
        viewer = client_for(tenancy.user_for("viewer"))
        response = viewer.post(f"/w/{tenancy.workspace.pk}/settings/channels/{connection.pk}/test-email/")
        assert response.status_code == 403


class TestTheConnectRouteIsRegistered:
    def test_the_generic_form_refuses_email_now_that_a_guided_flow_exists(
        self, admin_client: Client, tenancy: Any
    ) -> None:
        """``CONNECT_ROUTES`` drives both the link and the form's refusal."""
        response = admin_client.post(
            f"/w/{tenancy.workspace.pk}/settings/channels/new/",
            {"platform": Platform.EMAIL.value, "display_name": "Manual", "external_id": "manual.test"},
        )
        assert response.status_code == 200
        assert connections(tenancy).count() == 0

    def test_the_list_links_to_the_guided_flow(self, admin_client: Client, tenancy: Any) -> None:
        response = admin_client.get(f"/w/{tenancy.workspace.pk}/settings/channels/")
        assert b"email/connect/" in response.content
        # And no longer names the issue that was going to build it.
        assert b"#21 (L5-E)" not in response.content
