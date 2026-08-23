"""No token material in logs (SECURITY-BASELINE §5).

A WhatsApp system user token *is* the business: anyone holding it can read every
message the number receives and send as it. Three layers have to hold, and each
is asserted separately, because any one of them passing alone would make the
other two look unnecessary:

1. the token travels in an ``Authorization`` header rather than Graph's
   ``access_token`` query parameter, so it is not in a URL httpx logs at INFO;
2. ``request_json`` reports the **host** of a failed call and never the path,
   and this adapter never logs a URL, a token or Meta's prose itself;
3. ``apps.common.logging`` scrubs the ``EAA…`` shape and ``token=``-style values
   out of whatever still gets through.
"""

import logging
from typing import Any

import pytest

from apps.channels.events import OutboundMessage, TextBlock
from apps.channels.models import ChannelConnection
from apps.channels.providers.exceptions import APIError
from apps.channels.providers.whatsapp import WhatsAppAdapter, call, credentials_of
from apps.channels.tests.whatsapp_support import ACCESS_TOKEN, Reply, fake_graph_api, make_connection
from apps.common.logging import REDACTED, scrub

pytestmark = pytest.mark.django_db

TEXT = OutboundMessage(blocks=(TextBlock(text="hello"),))


class Identity:
    def __init__(self, address: str = "+447700900123") -> None:
        self.platform_user_id = address


@pytest.fixture
def connection(tenancy: Any) -> ChannelConnection:
    return make_connection(tenancy.workspace)


def emitted(caplog: Any) -> str:
    """Everything logged during the block, as one string."""
    return "\n".join(f"{record.getMessage()} {record.exc_text or ''}" for record in caplog.records)


class TestTheTokenStaysOutOfTheRequestLine:
    def test_it_travels_in_a_header(self, connection: ChannelConnection) -> None:
        with fake_graph_api() as fake:
            WhatsAppAdapter().send(connection, Identity(), TEXT)
        assert fake.authorizations == [f"Bearer {ACCESS_TOKEN}"]

    def test_it_is_in_neither_the_path_nor_the_query(self, connection: ChannelConnection) -> None:
        """Graph accepts ``?access_token=``, and httpx logs every URL it requests."""
        with fake_graph_api() as fake:
            WhatsAppAdapter().send(connection, Identity(), TEXT)
        assert all(ACCESS_TOKEN not in path for path in fake.paths())
        assert all(ACCESS_TOKEN not in str(query) for query in fake.queries)


class TestNothingLogsTheToken:
    def test_a_failed_call_names_the_host_and_nothing_else(self, connection: ChannelConnection, caplog: Any) -> None:
        with caplog.at_level(logging.DEBUG), fake_graph_api() as fake:
            fake.reply("messages", Reply(status=400))
            with pytest.raises(APIError) as caught:
                WhatsAppAdapter().send(connection, Identity(), TEXT)

        assert "graph.facebook.com" in str(caught.value)
        assert ACCESS_TOKEN not in str(caught.value)
        assert ACCESS_TOKEN not in emitted(caplog)

    def test_metas_prose_never_reaches_the_error(self, connection: ChannelConnection) -> None:
        """A provider's error text routinely quotes the request that caused it."""
        with fake_graph_api() as fake:
            fake.reply("messages", Reply(status=400))
            with pytest.raises(APIError) as caught:
                WhatsAppAdapter().send(connection, Identity(), TEXT)
        assert "Fake failure" not in str(caught.value)

    def test_a_transport_failure_does_not_leak_the_url(self, connection: ChannelConnection, caplog: Any) -> None:
        """httpx puts the full URL, query string and all, into transport errors."""
        import httpx

        def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("failed to connect", request=request)

        client = httpx.Client(transport=httpx.MockTransport(boom))
        from apps.channels.providers import whatsapp

        original = whatsapp._client
        whatsapp._client = lambda: client  # type: ignore[assignment]
        try:
            with caplog.at_level(logging.DEBUG), pytest.raises(APIError) as caught:
                WhatsAppAdapter().send(connection, Identity(), TEXT)
        finally:
            whatsapp._client = original  # type: ignore[assignment]
            client.close()

        assert ACCESS_TOKEN not in str(caught.value)
        assert ACCESS_TOKEN not in emitted(caplog)

    def test_a_decryption_failure_logs_no_token(self, connection: ChannelConnection, caplog: Any) -> None:
        """``credentials`` is encrypted, so reading it can fail on a rotated key."""

        class Broken:
            pk = connection.pk

            @property
            def credentials(self) -> Any:
                raise ValueError("bad key")

        with caplog.at_level(logging.DEBUG):
            assert credentials_of(Broken()) == {}  # type: ignore[arg-type]
        assert ACCESS_TOKEN not in emitted(caplog)

    def test_a_failed_disconnect_logs_no_token(self, connection: ChannelConnection, caplog: Any) -> None:
        with caplog.at_level(logging.DEBUG), fake_graph_api() as fake:
            fake.reply("subscribed_apps", Reply(status=400))
            with pytest.raises(APIError):
                WhatsAppAdapter().on_disconnect(connection)
        assert ACCESS_TOKEN not in emitted(caplog)


class TestTheScrubberKnowsThisShape:
    def test_a_bare_meta_token_is_redacted(self) -> None:
        assert ACCESS_TOKEN not in scrub(f"the token is {ACCESS_TOKEN} apparently")

    def test_it_survives_a_bearer_header(self) -> None:
        assert scrub(f"Authorization: Bearer {ACCESS_TOKEN}").endswith(REDACTED)

    def test_a_keyed_value_is_redacted(self) -> None:
        assert ACCESS_TOKEN not in scrub(f'{{"access_token": "{ACCESS_TOKEN}"}}')

    def test_the_scrubber_runs_on_real_records(self, caplog: Any) -> None:
        """Installed as a record factory, so it covers handlers a filter misses."""
        with caplog.at_level(logging.DEBUG):
            logging.getLogger(__name__).info("token=%s", ACCESS_TOKEN)
        assert ACCESS_TOKEN not in emitted(caplog)

    def test_the_call_helper_refuses_an_empty_token_rather_than_calling(self) -> None:
        with pytest.raises(APIError, match="no access token"):
            call("", "1/messages", {})
