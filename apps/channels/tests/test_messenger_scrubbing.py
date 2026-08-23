"""No Meta credential material in logs (SECURITY-BASELINE §5).

Two credentials, and they fail differently.

The **page access token** *is* the page: anyone holding it can read every message
sent to it and send as it. Unlike Telegram's bot token it never appears in a URL,
because ``meta_common.graph_call`` puts it in an ``Authorization`` header — so the
first assertion here is that the design holds, not merely that the scrubber
catches it afterwards.

The **app secret** is what signs every inbound delivery for the whole deployment.
It never reaches an outbound call at all except once, in the OAuth token exchange,
which is POSTed with a form body for exactly this reason: ``httpx`` logs the URL
of every request it makes at INFO, and Meta's own documentation puts the secret in
a query string.

Four layers, asserted separately, because any one of them passing alone would make
the other three look unnecessary:

1. the token travels in a header and the secret in a body, so neither is ever in a
   URL;
2. ``request_json`` reports the **host** of a failed call and never the path;
3. this adapter never logs a URL, a token or a provider's prose itself;
4. ``apps.common.logging`` scrubs Meta's ``EAA…`` shape and ``…secret=``-style
   values out of whatever still gets through.
"""

import logging
from typing import Any

import pytest

from apps.channels.events import OutboundMessage, TextBlock
from apps.channels.models import ChannelConnection
from apps.channels.providers import meta_common
from apps.channels.providers.exceptions import APIError
from apps.channels.providers.messenger import MessengerAdapter, list_posts
from apps.channels.tests.messenger_support import APP_SECRET, PAGE_TOKEN, PSID, Reply, fake_graph
from apps.common.logging import scrub

pytestmark = pytest.mark.django_db

TEXT = OutboundMessage(blocks=(TextBlock(text="Hi"),))


class Identity:
    def __init__(self, psid: str = PSID) -> None:
        self.platform_user_id = psid
        self.contact = None


def emitted(caplog: Any) -> str:
    """Everything logged during the block, as one string."""
    return "\n".join(f"{record.getMessage()} {record.exc_text or ''}" for record in caplog.records)


class TestTheTokenIsNeverInAUrl:
    def test_a_send_puts_it_in_the_authorization_header(self, page: ChannelConnection) -> None:
        with fake_graph() as graph:
            MessengerAdapter().send(page, Identity(), TEXT)
        (call,) = graph.calls
        assert call.authorization == f"Bearer {PAGE_TOKEN}"
        assert PAGE_TOKEN not in call.path
        assert PAGE_TOKEN not in str(call.params)

    def test_so_does_every_other_graph_call_this_adapter_makes(self, page: ChannelConnection) -> None:
        """Listing posts, subscribing, the messenger profile, the comment edges."""
        from apps.channels.providers.messenger import set_get_started, subscribe_page, unsubscribe_page

        with fake_graph() as graph:
            subscribe_page(page)
            unsubscribe_page(page)
            set_get_started(page)
            list_posts(page, 5)
            MessengerAdapter().send_typing(page, Identity())
        assert graph.calls
        for call in graph.calls:
            assert call.authorization == f"Bearer {PAGE_TOKEN}"
            assert PAGE_TOKEN not in call.path
            assert PAGE_TOKEN not in str(call.params)


class TestNothingLogsACredential:
    @pytest.fixture(autouse=True)
    def _capture(self, caplog: Any) -> None:
        caplog.set_level(logging.DEBUG)

    def test_a_failed_call_names_the_host_and_nothing_else(self, page: ChannelConnection, caplog: Any) -> None:
        def configure(graph: Any) -> None:
            graph.reply("/messages", Reply(status=500))

        with fake_graph(configure), pytest.raises(APIError) as caught:
            MessengerAdapter().send(page, Identity(), TEXT)

        message = str(caught.value)
        assert "graph.facebook.com" in message
        assert PAGE_TOKEN not in message
        assert page.external_id not in message
        assert PAGE_TOKEN not in emitted(caplog)

    def test_a_transport_failure_does_not_leak_the_url(self, page: ChannelConnection, caplog: Any) -> None:
        """``httpx`` puts the full URL into transport error messages.

        ``request_json`` reports ``type(exc).__name__`` instead, which is why the
        exception below carries a class name rather than an address.
        """
        import httpx

        def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("failed connecting to graph.facebook.com", request=request)

        client = httpx.Client(transport=httpx.MockTransport(boom))
        original = meta_common._client
        meta_common._client = lambda: client  # type: ignore[assignment]
        try:
            with pytest.raises(APIError) as caught:
                MessengerAdapter().send(page, Identity(), TEXT)
        finally:
            meta_common._client = original  # type: ignore[assignment]
            client.close()

        assert PAGE_TOKEN not in str(caught.value)
        assert PAGE_TOKEN not in emitted(caplog)

    def test_a_swallowed_courtesy_failure_logs_no_token(self, page: ChannelConnection, caplog: Any) -> None:
        def configure(graph: Any) -> None:
            graph.reply("/messages", Reply(status=500))

        with fake_graph(configure):
            MessengerAdapter().send_typing(page, Identity())
            MessengerAdapter().mark_seen(page, Identity())
        assert PAGE_TOKEN not in emitted(caplog)

    def test_a_decryption_failure_logs_no_token(self, page: ChannelConnection, caplog: Any) -> None:
        """A deployment whose key changed reads as "no token", loudly but safely."""

        class Broken:
            pk = page.pk

            @property
            def credentials(self) -> Any:
                raise ValueError("could not decrypt")

        assert meta_common.page_token(Broken()) == ""  # type: ignore[arg-type]
        assert PAGE_TOKEN not in emitted(caplog)

    def test_a_failed_post_listing_logs_no_token(self, page: ChannelConnection, caplog: Any) -> None:
        from apps.channels.posts import PostListingError

        def configure(graph: Any) -> None:
            graph.reply("/posts", Reply(status=400))

        with fake_graph(configure), pytest.raises(PostListingError):
            list_posts(page, 5)
        assert PAGE_TOKEN not in emitted(caplog)

    def test_a_dead_token_notification_carries_no_token(self, page: ChannelConnection, caplog: Any) -> None:
        """The notification names the channel, which is a display string, not a credential."""
        from apps.notifications.models import Notification

        def configure(graph: Any) -> None:
            graph.reply("/messages", Reply(status=400, body={"error": {"message": "bad", "code": 190}}))

        with fake_graph(configure), pytest.raises(APIError):
            MessengerAdapter().send(page, Identity(), TEXT)

        rendered = "\n".join(f"{row.title} {row.body}" for row in Notification.objects.all())
        assert PAGE_TOKEN not in rendered
        assert APP_SECRET not in rendered
        assert PAGE_TOKEN not in emitted(caplog)


class TestTheScrubberKnowsMetasShape:
    """Layer four: what the pattern catches if all of the above ever slipped."""

    def test_a_page_token_is_redacted(self) -> None:
        assert PAGE_TOKEN not in scrub(f"connecting with {PAGE_TOKEN} now")
        assert "[REDACTED]" in scrub(f"connecting with {PAGE_TOKEN} now")

    def test_it_survives_a_query_string(self) -> None:
        """The shape Meta's own documentation writes, and the one to defend."""
        line = f"GET https://graph.facebook.com/v21.0/me?access_token={PAGE_TOKEN}"
        assert PAGE_TOKEN not in scrub(line)

    def test_a_bearer_header_is_redacted(self) -> None:
        assert PAGE_TOKEN not in scrub(f"Authorization: Bearer {PAGE_TOKEN}")

    def test_an_app_secret_is_redacted_by_its_key(self) -> None:
        assert APP_SECRET not in scrub(f"client_secret={APP_SECRET}")
        assert APP_SECRET not in scrub(f'{{"app_secret": "{APP_SECRET}"}}')

    def test_the_scrubber_runs_on_real_records(self, caplog: Any) -> None:
        """Installed as a record factory, so a handler nobody configured is covered."""
        caplog.set_level(logging.DEBUG)
        logging.getLogger(__name__).info("page token %s", PAGE_TOKEN)
        assert PAGE_TOKEN not in emitted(caplog)

    def test_ordinary_prose_beginning_with_eaa_is_left_alone(self) -> None:
        """The pattern needs 20+ characters after ``EAA``, so it is not a wildcard."""
        assert scrub("EAA short") == "EAA short"
