"""Sending on Instagram: the wire payloads, and what happens when Meta says no.

Two halves, and the split is deliberate.

:func:`apps.channels.providers.instagram.wire_messages` is **pure** — no HTTP, no
database, no clock — so the payload assertions below are a table a reader can
check against Meta's reference without reading the send loop. That is the same
property ``telegram.wire_calls`` has and for the same reason.

The rest goes through the real ``send``, the real ``request_json`` and the real
error mapping, with only the socket replaced. A 429 really does become a
``RateLimitError`` carrying ``retry_after``; a body with ``error.code`` 190
really does reach ``APIError.code`` and flip the connection.
"""

import logging
from types import SimpleNamespace
from typing import Any

import pytest

from apps.channels.events import (
    Button,
    Card,
    CardBlock,
    GalleryBlock,
    MediaBlock,
    OutboundMessage,
    QuickReply,
    TextBlock,
)
from apps.channels.models import ChannelConnection, ConnectionStatus
from apps.channels.providers.exceptions import APIError, RateLimitError
from apps.channels.providers.instagram import (
    MAX_TEXT_BYTES,
    MAX_TITLE_CHARS,
    InstagramAdapter,
    wire_messages,
)
from apps.channels.tests.instagram_support import ACCESS_TOKEN, IG_USER_ID, Reply, fake_graph
from apps.notifications.models import Notification

pytestmark = pytest.mark.django_db

TO = {"id": IG_USER_ID}


def identity(**kwargs: Any) -> Any:
    """A stand-in for L3-A's ContactChannelIdentity, with no database behind it.

    ``last_inbound_at`` set by default, because that is what tells the adapter
    this is an established thread and no private-reply lookup is needed. The
    comment tests are the ones that leave it None on purpose.
    """
    defaults: dict[str, Any] = {
        "platform_user_id": IG_USER_ID,
        "last_inbound_at": "2026-08-01T00:00:00Z",
        "contact": None,
    }
    return SimpleNamespace(**{**defaults, **kwargs})


def send(connection: ChannelConnection, outbound: OutboundMessage, **kwargs: Any) -> Any:
    return InstagramAdapter().send(connection, identity(**kwargs), outbound)


def text(value: str) -> OutboundMessage:
    return OutboundMessage(blocks=(TextBlock(text=value),))


class TestWirePayloads:
    """The pure renderer, as a table."""

    def test_text(self) -> None:
        assert wire_messages(TO, text("Hello")) == [{"recipient": TO, "message": {"text": "Hello"}}]

    def test_a_blank_block_produces_nothing(self) -> None:
        """Meta rejects an empty ``text`` outright."""
        assert wire_messages(TO, text("   ")) == []

    def test_long_text_splits(self) -> None:
        bodies = wire_messages(TO, text("word " * 400))
        assert len(bodies) > 1
        for body in bodies:
            assert len(body["message"]["text"].encode("utf-8")) <= MAX_TEXT_BYTES

    def test_multibyte_text_is_split_on_bytes_not_characters(self) -> None:
        """Meta's cap is "1000 bytes or less". A thousand characters of Japanese
        is three thousand bytes, and a character-only cap would hand the platform
        a message it rejects — for one class of author and not another."""
        bodies = wire_messages(TO, text("あ" * 900))
        assert len(bodies) > 1
        for body in bodies:
            assert len(body["message"]["text"].encode("utf-8")) <= MAX_TEXT_BYTES

    def test_media_becomes_an_attachment_with_the_caption_after_it(self) -> None:
        """Meta's attachment payload has no caption field, so it gets its own bubble."""
        message = OutboundMessage(
            blocks=(MediaBlock(kind="image", url="https://cdn.test/a.png", caption="Our new kit"),)
        )
        first, second = wire_messages(TO, message)
        assert first["message"] == {"attachment": {"type": "image", "payload": {"url": "https://cdn.test/a.png"}}}
        assert second["message"] == {"text": "Our new kit"}

    def test_an_unsupported_media_kind_falls_back_to_text(self) -> None:
        """Instagram messaging has no generic document attachment."""
        message = OutboundMessage(blocks=(MediaBlock(kind="file", url="https://cdn.test/a.pdf", caption="Invoice"),))
        (body,) = wire_messages(TO, message)
        assert body["message"]["text"] == "Invoice\nhttps://cdn.test/a.pdf"

    def test_a_card_becomes_a_generic_template(self) -> None:
        card = Card(title="Berlin", subtitle="Ships Tuesday", image_url="https://cdn.test/b.png", url="https://a.test")
        (body,) = wire_messages(TO, OutboundMessage(blocks=(CardBlock(card=card),)))
        payload = body["message"]["attachment"]["payload"]
        assert payload["template_type"] == "generic"
        (element,) = payload["elements"]
        assert element["title"] == "Berlin"
        assert element["subtitle"] == "Ships Tuesday"
        assert element["default_action"] == {"type": "web_url", "url": "https://a.test"}

    def test_a_card_with_only_a_title_falls_back_to_text(self) -> None:
        """Meta requires "at least one property in addition to title"."""
        (body,) = wire_messages(TO, OutboundMessage(blocks=(CardBlock(card=Card(title="Just a title")),)))
        assert body["message"] == {"text": "Just a title"}

    def test_a_gallery_becomes_one_template_of_ten_at_most(self) -> None:
        cards = tuple(Card(title=f"Item {index}", subtitle="x") for index in range(23))
        bodies = wire_messages(TO, OutboundMessage(blocks=(GalleryBlock(cards=cards),)))
        counts = [len(body["message"]["attachment"]["payload"]["elements"]) for body in bodies]
        assert counts == [10, 10, 3]

    def test_buttons_take_over_the_trailing_text(self) -> None:
        """No duplication and no invented copy: the tail of the author's own
        message becomes the card title the buttons sit under."""
        message = OutboundMessage(
            blocks=(TextBlock(text="Pick one"),),
            buttons=(Button(id="a", label="Alpha"), Button(id="b", label="Beta", url="https://b.test")),
            node_id="node-1",
        )
        (body,) = wire_messages(TO, message)
        (element,) = body["message"]["attachment"]["payload"]["elements"]
        assert element["title"] == "Pick one"
        assert element["buttons"] == [
            {"type": "postback", "title": "Alpha", "payload": "node-1:a"},
            {"type": "web_url", "url": "https://b.test", "title": "Beta"},
        ]

    def test_a_long_text_with_buttons_keeps_all_of_it(self) -> None:
        long_text = "word " * 60
        message = OutboundMessage(blocks=(TextBlock(text=long_text),), buttons=(Button(id="a", label="Alpha"),))
        head, tail = wire_messages(TO, message)
        (element,) = tail["message"]["attachment"]["payload"]["elements"]
        assert len(element["title"]) <= MAX_TITLE_CHARS
        # Nothing is duplicated and nothing is lost.
        assert head["message"]["text"] + " " + element["title"] == long_text.strip()

    def test_buttons_join_a_card_that_is_already_there(self) -> None:
        message = OutboundMessage(
            blocks=(CardBlock(card=Card(title="Berlin", subtitle="Ships Tuesday")),),
            buttons=(Button(id="a", label="Alpha"),),
        )
        (body,) = wire_messages(TO, message)
        (element,) = body["message"]["attachment"]["payload"]["elements"]
        assert element["buttons"] == [{"type": "postback", "title": "Alpha", "payload": ":a"}]

    def test_buttons_with_nothing_to_hang_them_on_are_left_out(self, caplog: Any) -> None:
        """Visible rather than silent — the same choice Telegram makes for a
        button it cannot represent."""
        message = OutboundMessage(
            blocks=(MediaBlock(kind="image", url="https://cdn.test/a.png"),),
            buttons=(Button(id="a", label="Alpha"),),
        )
        with caplog.at_level(logging.WARNING):
            (body,) = wire_messages(TO, message)
        assert "attachment" in body["message"]
        assert "left out" in caplog.text

    def test_quick_replies_ride_on_the_last_text(self) -> None:
        message = OutboundMessage(
            blocks=(TextBlock(text="Coffee?"),),
            quick_replies=(QuickReply(id="y", label="Yes"), QuickReply(id="n", label="No")),
            node_id="node-2",
        )
        (body,) = wire_messages(TO, message)
        assert body["message"]["text"] == "Coffee?"
        assert body["message"]["quick_replies"] == [
            {"content_type": "text", "title": "Yes", "payload": "node-2:y"},
            {"content_type": "text", "title": "No", "payload": "node-2:n"},
        ]

    def test_a_quick_reply_label_is_cut_to_twenty(self) -> None:
        """Instagram truncates at twenty; cutting knowingly beats cutting mid-word."""
        message = OutboundMessage(quick_replies=(QuickReply(id="y", label="y" * 40),), blocks=(TextBlock(text="Hi"),))
        (body,) = wire_messages(TO, message)
        assert len(body["message"]["quick_replies"][0]["title"]) == 20

    def test_quick_replies_with_no_text_are_dropped_not_numbered(self, caplog: Any) -> None:
        """Meta accepts ``quick_replies`` only beside ``message.text``, and there
        is none. Numbering them here *looked* like the shared numbered-option
        fallback and was not one: that fallback works because the engine rebuilds
        the number-to-id map by re-running ``downgrade``, which produces no
        numbers for a platform declaring ``quick_replies=True``. The contact was
        being shown instructions that resolved to nothing."""
        message = OutboundMessage(
            blocks=(MediaBlock(kind="image", url="https://cdn.test/a.png"),),
            quick_replies=(QuickReply(id="y", label="Yes"),),
        )
        with caplog.at_level(logging.WARNING):
            (body,) = wire_messages(TO, message)
        assert "attachment" in body["message"]
        assert "Reply 1" not in str(body)
        assert "left out" in caplog.text

    def test_a_media_caption_gives_quick_replies_somewhere_to_ride(self) -> None:
        """Which is what the warning tells an author to do."""
        message = OutboundMessage(
            blocks=(MediaBlock(kind="image", url="https://cdn.test/a.png", caption="Pick one"),),
            quick_replies=(QuickReply(id="y", label="Yes"),),
        )
        _, caption = wire_messages(TO, message)
        assert caption["message"]["text"] == "Pick one"
        assert caption["message"]["quick_replies"][0]["title"] == "Yes"

    def test_the_split_fallback_never_emits_an_over_cap_piece(self, caplog: Any) -> None:
        """The depth guard used to return the chunk unsplit, so Meta answered 400
        and the send pipeline reported a bare ``provider_rejected`` naming
        nothing about length. It cuts on the encoded bytes instead, and says so.
        """
        from apps.channels.providers.instagram import _within_bytes

        chunk = "\U0001f600" * 400
        with caplog.at_level(logging.WARNING):
            pieces = _within_bytes(chunk, depth=4)
        assert len(pieces) == 1
        assert len(pieces[0].encode("utf-8")) <= MAX_TEXT_BYTES
        # Cut on a character boundary, not mid-codepoint.
        assert pieces[0] == pieces[0].encode("utf-8").decode("utf-8")
        assert "cut to fit" in caplog.text

    def test_card_buttons_carry_the_node_id_like_message_buttons(self) -> None:
        """Two halves of one message encoding differently is the kind of
        inconsistency that costs somebody an afternoon later."""
        card = Card(title="Berlin", subtitle="Ships Tuesday", buttons=(Button(id="buy", label="Buy"),))
        message = OutboundMessage(
            blocks=(CardBlock(card=card),),
            buttons=(Button(id="more", label="More"),),
            node_id="node-4",
        )
        (body,) = wire_messages(TO, message)
        (element,) = body["message"]["attachment"]["payload"]["elements"]
        assert [item["payload"] for item in element["buttons"]] == ["node-4:buy", "node-4:more"]

    def test_a_tag_becomes_messaging_type_message_tag(self) -> None:
        """The tag is the compliance engine's, never invented here (SPEC §22)."""
        (body,) = wire_messages(TO, OutboundMessage(blocks=(TextBlock(text="Hi"),), tag="HUMAN_AGENT"))
        assert body["messaging_type"] == "MESSAGE_TAG"
        assert body["tag"] == "HUMAN_AGENT"

    def test_no_tag_means_no_messaging_type(self) -> None:
        (body,) = wire_messages(TO, text("Hi"))
        assert "messaging_type" not in body

    def test_a_private_reply_differs_only_in_the_recipient(self) -> None:
        by_id = wire_messages({"id": IG_USER_ID}, text("Hi"))
        by_comment = wire_messages({"comment_id": "17900000000000101"}, text("Hi"))
        assert by_id[0]["message"] == by_comment[0]["message"]
        assert by_comment[0]["recipient"] == {"comment_id": "17900000000000101"}


class TestSending:
    def test_a_send_calls_the_messages_endpoint(self, instagram_connection: ChannelConnection) -> None:
        with fake_graph() as graph:
            result = send(instagram_connection, text("Hello"))
        assert result.status == "sent"
        assert result.provider_message_id == "mid.sent.1"
        assert graph.paths() == ["me/messages"]
        assert graph.bodies("me/messages")[0]["recipient"] == {"id": IG_USER_ID}

    def test_the_token_travels_in_a_header_not_the_url(self, instagram_connection: ChannelConnection) -> None:
        """A query-string token lands in every proxy access log for ever."""
        with fake_graph() as graph:
            send(instagram_connection, text("Hello"))
        assert graph.tokens == [f"Bearer {ACCESS_TOKEN}"]

    def test_no_identity_fails_rather_than_raising(self, instagram_connection: ChannelConnection) -> None:
        result = send(instagram_connection, text("Hello"), platform_user_id="")
        assert result.status == "failed"
        assert result.error == "no_recipient"

    def test_an_unsendable_message_is_reported_not_silently_counted(
        self, instagram_connection: ChannelConnection
    ) -> None:
        with fake_graph() as graph:
            result = send(instagram_connection, OutboundMessage(blocks=(TextBlock(text="   "),)))
        assert result.status == "failed"
        assert result.error == "empty_message"
        assert graph.calls == []

    def test_a_connection_with_no_token_fails_by_name(self, instagram_connection: ChannelConnection) -> None:
        instagram_connection.credentials = {}  # type: ignore[assignment]
        instagram_connection.save(update_fields=["credentials", "updated_at"])
        with fake_graph(), pytest.raises(APIError, match="no access token"):
            send(instagram_connection, text("Hello"))

    def test_a_gallery_sends_one_call_per_template(self, instagram_connection: ChannelConnection) -> None:
        cards = tuple(Card(title=f"Item {index}", subtitle="x") for index in range(12))
        with fake_graph() as graph:
            send(instagram_connection, OutboundMessage(blocks=(GalleryBlock(cards=cards),)))
        assert len(graph.bodies("me/messages")) == 2

    def test_typing_and_seen_are_sender_actions(self, instagram_connection: ChannelConnection) -> None:
        adapter = InstagramAdapter()
        with fake_graph() as graph:
            adapter.send_typing(instagram_connection, identity())
            adapter.mark_seen(instagram_connection, identity())
        assert [body["sender_action"] for body in graph.bodies("me/messages")] == ["typing_on", "mark_seen"]

    def test_a_failed_courtesy_call_is_swallowed(self, instagram_connection: ChannelConnection) -> None:
        """Cosmetic. It must not cost the contact their reply."""
        with fake_graph(lambda graph: graph.reply("me/messages", Reply(status=400))):
            InstagramAdapter().send_typing(instagram_connection, identity())


class TestErrors:
    def test_429_becomes_a_rate_limit_error_with_retry_after(self, instagram_connection: ChannelConnection) -> None:
        def configure(graph: Any) -> None:
            graph.reply("me/messages", Reply(status=429, headers={"Retry-After": "42"}))

        with fake_graph(configure), pytest.raises(RateLimitError) as raised:
            send(instagram_connection, text("Hello"))
        assert raised.value.retry_after == 42.0

    def test_a_dead_token_flips_the_connection_and_notifies(
        self, instagram_connection: ChannelConnection, tenancy: Any
    ) -> None:
        def configure(graph: Any) -> None:
            graph.reply(
                "me/messages",
                Reply(status=400, body={"error": {"message": "Session expired", "code": 190}}),
            )

        with fake_graph(configure), pytest.raises(APIError):
            send(instagram_connection, text("Hello"))

        instagram_connection.refresh_from_db()
        assert instagram_connection.status == ConnectionStatus.NEEDS_REAUTH
        assert Notification.objects.filter(event_type="channel_needs_reauth").exists()

    def test_the_reauth_notification_fires_only_on_the_transition(
        self, instagram_connection: ChannelConnection
    ) -> None:
        """A dead token is retried by every send and by every hourly sweep. One
        notification per attempt is how an operator learns to ignore the bell.

        Counted across attempts rather than as an absolute: one *round* fans out
        to every workspace admin, which is the notification engine's business.
        """

        def configure(graph: Any) -> None:
            graph.reply("me/messages", Reply(status=400, body={"error": {"code": 190}}))

        rows = Notification.objects.filter(event_type="channel_needs_reauth")
        with fake_graph(configure):
            with pytest.raises(APIError):
                send(instagram_connection, text("Hello"))
            after_first = rows.count()
            for _ in range(3):
                with pytest.raises(APIError):
                    send(instagram_connection, text("Hello"))

        assert after_first > 0
        assert rows.count() == after_first

    def test_an_unreachable_contact_records_an_opt_out(self, instagram_connection: ChannelConnection) -> None:
        """The adapter raises the event rather than writing ``opted_out_at``:
        ROADMAP contract 3 gives that column exactly one write site."""
        seen: list[Any] = []
        from apps.channels import ingest as channels_ingest

        channels_ingest.register_processor(lambda _c, events: seen.extend(events), name="spy")

        def configure(graph: Any) -> None:
            graph.reply("me/messages", Reply(status=400, body={"error": {"code": 551}}))

        with fake_graph(configure), pytest.raises(APIError):
            send(instagram_connection, text("Hello"))

        assert [event.type for event in seen] == ["opt_out"]
        assert seen[0].platform_user_id == IG_USER_ID

    def test_an_ordinary_rejection_changes_nothing_structural(self, instagram_connection: ChannelConnection) -> None:
        def configure(graph: Any) -> None:
            graph.reply("me/messages", Reply(status=400, body={"error": {"code": 100}}))

        with fake_graph(configure), pytest.raises(APIError):
            send(instagram_connection, text("Hello"))
        instagram_connection.refresh_from_db()
        assert instagram_connection.status == ConnectionStatus.ACTIVE


class TestSecrets:
    def test_no_token_reaches_a_log_on_any_failure(self, instagram_connection: ChannelConnection, caplog: Any) -> None:
        """SECURITY-BASELINE §5. ``request_json`` reports the host of a failed
        call and never the path, and nothing here logs a token."""

        def configure(graph: Any) -> None:
            graph.reply("me/messages", Reply(status=400, body={"error": {"code": 190}}))

        with caplog.at_level(logging.DEBUG), fake_graph(configure), pytest.raises(APIError):
            send(instagram_connection, text("Hello"))
        assert ACCESS_TOKEN not in caplog.text
        assert "Bearer" not in caplog.text

    def test_an_api_error_names_the_host_and_not_the_path(self, instagram_connection: ChannelConnection) -> None:
        def configure(graph: Any) -> None:
            graph.reply("me/messages", Reply(status=400, body={"error": {"code": 100}}))

        with fake_graph(configure), pytest.raises(APIError) as raised:
            send(instagram_connection, text("Hello"))
        assert "graph.instagram.com" in str(raised.value)
        assert "me/messages" not in str(raised.value)
