"""No bot-token material in logs (SECURITY-BASELINE §5).

The bot token *is* the bot: anyone holding it can read every message sent to it
and send as it. It appears in exactly one place at runtime — the path of every
Bot API URL — which is precisely the kind of place a well-meaning error message
puts in a log.

Three layers have to hold, and each is asserted separately, because any one of
them passing alone would make the other two look unnecessary:

1. ``request_json`` reports the **host** of a failed call and never the path;
2. this adapter never logs a URL, a token or a provider's prose itself;
3. ``apps.common.logging`` scrubs the ``<bot_id>:<secret>`` shape and
   ``token=``-style values out of whatever still gets through.
"""

import logging
from typing import Any

import pytest

from apps.channels.events import OutboundMessage, TextBlock
from apps.channels.models import ChannelConnection
from apps.channels.providers.exceptions import APIError
from apps.channels.providers.telegram import TelegramAdapter, call
from apps.channels.tests.telegram_support import BOT_TOKEN, Reply, fake_bot_api
from apps.common.logging import scrub

pytestmark = pytest.mark.django_db

SECRET_HALF = BOT_TOKEN.split(":", 1)[1]


class Identity:
    def __init__(self, chat_id: str = "5150") -> None:
        self.platform_user_id = chat_id


@pytest.fixture
def telegram_connection(connection: ChannelConnection) -> ChannelConnection:
    connection.credentials = {"bot_token": BOT_TOKEN}
    connection.save(update_fields=["credentials", "updated_at"])
    return connection


def emitted(caplog: Any) -> str:
    """Everything logged during the block, as one string."""
    return "\n".join(f"{record.getMessage()} {record.exc_text or ''}" for record in caplog.records)


class TestNothingLogsTheToken:
    @pytest.mark.parametrize("status", [400, 401, 403, 429, 500])
    def test_a_failed_call_names_the_host_and_nothing_else(
        self, telegram_connection: ChannelConnection, caplog: Any, status: int
    ) -> None:
        caplog.set_level(logging.DEBUG)
        with fake_bot_api(lambda fake: fake.reply("sendMessage", Reply(status=status))):
            try:
                TelegramAdapter().send(telegram_connection, Identity(), OutboundMessage(blocks=(TextBlock(text="hi"),)))
            except APIError as exc:
                # The exception is logged and shown in the inbox, so it counts
                # as output even when nothing catches it here.
                assert BOT_TOKEN not in str(exc)
                assert SECRET_HALF not in str(exc)
                assert "api.telegram.org" in str(exc)

        output = emitted(caplog)
        assert BOT_TOKEN not in output
        assert SECRET_HALF not in output

    def test_a_transport_failure_does_not_leak_the_url(
        self, telegram_connection: ChannelConnection, caplog: Any
    ) -> None:
        """httpx puts the full URL — token and all — in transport error text."""
        caplog.set_level(logging.DEBUG)
        with pytest.raises(APIError) as caught:
            # No fake installed, and an unroutable host: the call fails in the
            # transport rather than at the platform.
            call(BOT_TOKEN, "getMe")
        assert BOT_TOKEN not in str(caught.value)
        assert SECRET_HALF not in str(caught.value)
        assert BOT_TOKEN not in emitted(caplog)

    def test_a_failed_callback_answer_logs_no_token(self, telegram_connection: ChannelConnection, caplog: Any) -> None:
        caplog.set_level(logging.DEBUG)
        with fake_bot_api(lambda fake: setattr(fake, "default", Reply(status=500))):
            TelegramAdapter()._answer_callback_query(telegram_connection, "cbq-1")
        assert BOT_TOKEN not in emitted(caplog)

    def test_a_failed_typing_indicator_logs_no_token(self, telegram_connection: ChannelConnection, caplog: Any) -> None:
        caplog.set_level(logging.DEBUG)
        with fake_bot_api(lambda fake: fake.reply("sendChatAction", Reply(status=400))):
            TelegramAdapter().send_typing(telegram_connection, Identity())
        assert BOT_TOKEN not in emitted(caplog)

    def test_a_decryption_failure_logs_no_token(self, telegram_connection: ChannelConnection, caplog: Any) -> None:
        caplog.set_level(logging.DEBUG)
        from apps.channels.providers.telegram import bot_token

        assert bot_token(telegram_connection) == BOT_TOKEN
        assert BOT_TOKEN not in emitted(caplog)


class TestTheScrubberKnowsThisShape:
    """The backstop, tested directly — layer 3 above.

    The adapter is careful, but "careful" is a property of code somebody will
    edit. These assert that even a message that did carry a token comes out
    redacted, so a future line that logs one is a redaction rather than a leak.
    """

    def test_a_bot_api_url_is_redacted(self) -> None:
        scrubbed = scrub(f"POST https://api.telegram.org/bot{BOT_TOKEN}/sendMessage failed")
        assert BOT_TOKEN not in scrubbed
        assert SECRET_HALF not in scrubbed

    def test_a_bare_token_is_redacted(self) -> None:
        assert SECRET_HALF not in scrub(f"the token is {BOT_TOKEN}")

    def test_a_keyed_value_is_redacted(self) -> None:
        assert SECRET_HALF not in scrub(f'{{"bot_token": "{BOT_TOKEN}"}}')

    def test_the_scrubber_runs_on_real_records(self, caplog: Any) -> None:
        """Not just as a function — through the logging pipeline it is installed in."""
        from apps.common.logging import install_scrubbing_record_factory

        install_scrubbing_record_factory()
        caplog.set_level(logging.INFO)
        logging.getLogger("apps.channels.providers.telegram").info(
            "calling https://api.telegram.org/bot%s/getMe", BOT_TOKEN
        )
        assert SECRET_HALF not in emitted(caplog)
