"""Twilio credentials must not reach a log (SECURITY-BASELINE §5).

The threat here is narrower than Telegram's and worth stating precisely, because
it decides what the scrubber does and does not try to match.

A Telegram bot token lives in the *path* of every Bot API URL, and httpx logs
the URL of every request it makes at INFO — so the token had to be pattern-
matched or it would reach a log by the most ordinary route there is. Twilio
authenticates with HTTP Basic instead: the auth token is only ever in an
``Authorization`` header, which the scrubber's auth-scheme rule already covers
and which nothing in this project formats into a message.

What *is* in Twilio's URL path is the **account SID**, which identifies the
account, so that has a pattern of its own. The auth token deliberately does not:
32 bare hex characters is also an MD5 digest, a dashless UUID and half the ids in
a webhook payload, and a pattern matching it would redact all of them. These
tests hold both halves — the SID is redacted, and the token never gets anywhere
near a log to need it.
"""

from typing import Any

import pytest
from django.test import Client

from apps.channels.models import ChannelConnection
from apps.channels.providers.sms import TwilioAdapter
from apps.channels.tests.sms_support import (
    ACCOUNT_SID,
    AUTH_TOKEN,
    FakeTwilio,
    Reply,
    fake_twilio,
    load_payload,
    signed_post,
    sms_connection,
)
from apps.common.logging import REDACTED, scrub
from tests.support import Tenancy

pytestmark = pytest.mark.django_db


class _Identity:
    platform_user_id = "+15557778888"


@pytest.fixture
def connection(tenancy: Tenancy) -> ChannelConnection:
    return sms_connection(tenancy.workspace)


class TestTheScrubber:
    def test_an_account_sid_in_a_url_is_redacted(self) -> None:
        """The shape httpx logs: the SID is a path segment of every REST call."""
        line = f"HTTP Request: POST https://api.twilio.com/2010-04-01/Accounts/{ACCOUNT_SID}/Messages.json"

        assert ACCOUNT_SID not in scrub(line)
        assert REDACTED in scrub(line)

    def test_an_api_key_sid_is_redacted_too(self) -> None:
        assert "SK" + "a" * 32 not in scrub("key SK" + "a" * 32)

    def test_an_auth_token_in_a_basic_header_is_redacted(self) -> None:
        """By the auth-scheme rule, which is where Twilio's token actually lives."""
        line = "Authorization: Basic QUMwMTIzOjlmOGU3ZDZjNWI0YTM5Mjg="

        assert "QUMwMTIz" not in scrub(line)

    def test_a_token_named_in_a_key_value_pair_is_redacted(self) -> None:
        assert AUTH_TOKEN not in scrub(f"auth_token={AUTH_TOKEN}")

    def test_an_ordinary_hex_digest_survives(self) -> None:
        """The reason the bare auth-token shape has no pattern: it is
        indistinguishable from an MD5, a dashless UUID, or a message id."""
        digest = "d41d8cd98f00b204e9800998ecf8427e"

        assert digest in scrub(f"checksum {digest}")

    def test_scrubbing_is_idempotent(self) -> None:
        once = scrub(f"Accounts/{ACCOUNT_SID}/Messages.json")

        assert scrub(once) == once


class TestNothingLogsACredential:
    def test_a_failed_send_logs_the_host_and_nothing_else(
        self, tenancy: Tenancy, connection: ChannelConnection, caplog: Any
    ) -> None:
        from apps.channels.providers.exceptions import APIError

        fake = FakeTwilio()
        fake.reply("Messages.json", Reply({"code": 21211, "message": f"bad token {AUTH_TOKEN}"}, status=400))

        with caplog.at_level("DEBUG"), fake_twilio(fake), pytest.raises(APIError):
            TwilioAdapter().send(connection, _Identity(), _text("Hi"))

        assert AUTH_TOKEN not in caplog.text
        assert ACCOUNT_SID not in caplog.text

    def test_a_rejected_delivery_logs_no_credential(
        self, client: Client, connection: ChannelConnection, caplog: Any
    ) -> None:
        with caplog.at_level("DEBUG"):
            signed_post(client, connection, load_payload("inbound_text"), token="wrong")

        assert AUTH_TOKEN not in caplog.text

    def test_the_module_never_formats_a_credential_into_a_message(self) -> None:
        """Structural, so it stays true for log lines nobody has written yet."""
        import ast
        import inspect

        from apps.channels.providers import sms as sms_module

        tree = ast.parse(inspect.getsource(sms_module))
        logged_args = [
            arg
            for call in ast.walk(tree)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr in {"debug", "info", "warning", "error", "exception"}
            for arg in call.args
        ]
        names = {
            node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
            for arg in logged_args
            for node in ast.walk(arg)
            if isinstance(node, ast.Call)
        }
        assert not names & {"auth_token", "account_sid", "sign", "_credential"}

    def test_the_token_is_never_put_in_a_url(self) -> None:
        """The Telegram trap, which Twilio's Basic auth avoids — asserted so a
        later refactor cannot reintroduce it."""
        import inspect

        from apps.channels.providers import sms as sms_module

        for line in inspect.getsource(sms_module).splitlines():
            if "API_ROOT" in line and 'f"' in line:
                assert "token" not in line


def _text(body: str) -> Any:
    from apps.channels.events import OutboundMessage, TextBlock

    return OutboundMessage(blocks=(TextBlock(text=body),))
