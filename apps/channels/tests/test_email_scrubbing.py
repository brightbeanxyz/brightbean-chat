"""No email credential, and no unsubscribe token, ever reaches a log.

The sibling of ``test_telegram_scrubbing.py``, and it exists for the same reason
that one does: the scrubber's patterns are only as good as the shapes somebody
remembered to add, and an adapter's credentials are exactly the shapes nobody
thinks about until they are in a log file.

Four secrets are in play here where Telegram had one — an SMTP password, a
Resend API key, a Svix signing secret and an SES key pair — plus the ``/u/``
token, which is a capability in its own right: whoever holds one can withdraw
somebody else's consent.
"""

import logging
from typing import Any

import pytest

from apps.channels.models import ChannelConnection
from apps.channels.providers import email_backends
from apps.channels.providers.exceptions import APIError
from apps.channels.tests.email_support import DummySMTPServer
from apps.channels.unsubscribe import mint_token
from apps.common.logging import scrub
from apps.common.platforms import Platform

pytestmark = pytest.mark.django_db

# Shaped like the real thing, so the patterns are actually exercised. The
# Telegram suite makes the same point: a token shaped like "secret" proves
# nothing about a pattern written for a token shaped like a token.
SMTP_PASSWORD = "S3cret-Mail-Password-9fj2"
RESEND_KEY = "re_AbCdEfGh_1234567890abcdefghij"
SIGNING_SECRET = "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw"
SES_KEY_ID = "AKIAIOSFODNN7EXAMPLE"
SES_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"


class TestThePatternsCoverEmailShapes:
    @pytest.mark.parametrize(
        "text",
        [
            f"Resend rejected the key {RESEND_KEY}",
            f'{{"api_key": "{RESEND_KEY}"}}',
            f"signing secret {SIGNING_SECRET} did not verify",
            f"aws key {SES_KEY_ID} is not allowed",
            f'password="{SMTP_PASSWORD}"',
            f"smtp_password={SMTP_PASSWORD}",
            f'{{"secret_access_key": "{SES_SECRET}"}}',
        ],
    )
    def test_a_credential_shape_is_redacted(self, text: str) -> None:
        cleaned = scrub(text)
        for secret in (RESEND_KEY, SIGNING_SECRET, SES_KEY_ID, SMTP_PASSWORD, SES_SECRET):
            assert secret not in cleaned
        assert "[REDACTED]" in cleaned

    def test_an_unsubscribe_url_path_is_redacted(self, tenancy: Any) -> None:
        """The one that reaches a log without anybody writing a log line.

        ``runserver``'s access log prints every path at INFO and
        ``django.request`` logs the path on a 500, so the token in a ``/u/`` URL
        gets there with no help at all.
        """

        class Identity:
            pk = "01a02e00-0000-7000-8000-000000000000"

        token = mint_token(Identity())
        cleaned = scrub(f'GET /u/{token}/ HTTP/1.1" 200')
        assert token not in cleaned
        assert "[REDACTED]" in cleaned

    def test_a_similar_path_is_left_alone(self) -> None:
        """The prefix is ``/u/`` exactly, not any path starting with a "u"."""
        assert scrub("GET /uploads/logo.png") == "GET /uploads/logo.png"
        assert scrub("GET /ui/toast/") == "GET /ui/toast/"


class TestNothingLogsThemInPractice:
    """The other half: the shapes are covered, and nothing emits them anyway."""

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

    def test_an_smtp_failure_does_not_leak_the_password(self, tenancy: Any, caplog: pytest.LogCaptureFixture) -> None:
        connection = self._connection(
            tenancy,
            {
                "provider": "smtp",
                "host": "127.0.0.1",
                "port": 1,
                "security": "none",
                "username": "postmaster",
                "password": SMTP_PASSWORD,
                "from_address": "hello@sender.test",
            },
        )
        with caplog.at_level(logging.DEBUG), pytest.raises(APIError) as caught:
            email_backends.verify_credentials(connection)

        assert SMTP_PASSWORD not in str(caught.value)
        assert SMTP_PASSWORD not in caplog.text

    def test_an_smtp_rejection_reports_a_code_not_the_servers_prose(self, tenancy: Any) -> None:
        """An SMTP rejection quotes the envelope back, addresses included."""
        with DummySMTPServer(rcpt_reply="550 No mailbox for reader@example.test here") as server:
            connection = self._connection(tenancy, server.credentials(password=SMTP_PASSWORD))
            with pytest.raises(APIError) as caught:
                email_backends.deliver(connection, _envelope())

        assert "reader@example.test" not in str(caught.value)
        assert caught.value.code == "550"

    def test_a_resend_failure_does_not_leak_the_key(
        self, tenancy: Any, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``request_json`` reports the host of a failed call and never the path."""
        connection = self._connection(
            tenancy, {"provider": "resend", "api_key": RESEND_KEY, "from_address": "hello@sender.test"}
        )
        from apps.channels.tests.email_support import resend_transport

        client = resend_transport(status=401, body={"message": "Invalid API key"})

        def call(method: str, url: str, **kwargs: Any) -> Any:
            from apps.channels.providers.base import request_json

            kwargs.pop("client", None)
            return request_json(method, url, client=client, **kwargs)

        monkeypatch.setattr(email_backends, "request_json", call)
        with caplog.at_level(logging.DEBUG), pytest.raises(APIError) as caught:
            email_backends.deliver(connection, _envelope())

        assert RESEND_KEY not in str(caught.value)
        assert RESEND_KEY not in caplog.text

    def test_a_ses_failure_does_not_leak_the_secret(
        self, tenancy: Any, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from apps.channels.tests.email_support import FakeSESClient

        connection = self._connection(
            tenancy,
            {
                "provider": "ses",
                "access_key_id": SES_KEY_ID,
                "secret_access_key": SES_SECRET,
                "region": "eu-west-1",
                "from_address": "hello@sender.test",
            },
        )
        failing = FakeSESClient(
            error={
                "Error": {"Code": "SignatureDoesNotMatch", "Message": f"key {SES_KEY_ID} secret {SES_SECRET}"},
                "ResponseMetadata": {"HTTPStatusCode": 403},
            }
        )
        monkeypatch.setattr(email_backends, "ses_client", lambda *a, **k: failing)

        with caplog.at_level(logging.DEBUG), pytest.raises(APIError) as caught:
            email_backends.deliver(connection, _envelope())

        # The message carries the provider's *code* and never its prose, which
        # here quotes both halves of the key pair back at us.
        assert SES_SECRET not in str(caught.value)
        assert SES_KEY_ID not in str(caught.value)
        assert SES_SECRET not in caplog.text

    def test_the_admin_never_renders_credentials(self, tenancy: Any) -> None:
        """``masked_credentials`` exists for this (CONTRIBUTING)."""
        from django.contrib import admin

        from apps.channels.models import ChannelConnection as Model

        registered = admin.site._registry.get(Model)
        assert registered is not None
        assert "credentials" not in registered.list_display


def _envelope() -> Any:
    return email_backends.Envelope(
        to="reader@example.test",
        subject="Subject",
        html="<p>Body</p>",
        text="Body",
        from_address="hello@sender.test",
        headers={"List-Unsubscribe": "<https://app.test/u/tok/>"},
        message_id="<a@sender.test>",
    )
