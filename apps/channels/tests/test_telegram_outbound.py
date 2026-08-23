"""What actually goes on the wire, for every block and button combination.

``wire_calls`` is pure, so most of this is a table: an ``OutboundMessage`` in,
the exact ``(method, payload)`` list Telegram would receive out. That is worth
having as a snapshot rather than as prose, because the failure mode of a wrong
payload is silent — a message the platform rejects at send time, in production,
on somebody else's account.

The downgrade cases go through :func:`~apps.channels.downgrade.downgrade` first,
which is what the adapter does, so they pin the *combination* of the shared
renderer and this adapter rather than either alone.
"""

from typing import Any

import pytest

from apps.channels.capabilities import capabilities_for
from apps.channels.downgrade import downgrade
from apps.channels.events import (
    Button,
    Card,
    CardBlock,
    GalleryBlock,
    MediaBlock,
    OutboundMessage,
    QuickReply,
    SendStatus,
    TextBlock,
)
from apps.channels.models import ChannelConnection
from apps.channels.providers.exceptions import APIError, RateLimitError
from apps.channels.providers.telegram import (
    MAX_CALLBACK_DATA_BYTES,
    MAX_CAPTION_CHARS,
    MAX_TEXT_CHARS,
    TelegramAdapter,
    _button_id,
    store_bot_token,
    wire_calls,
)
from apps.channels.tests.telegram_support import BOT_TOKEN, Reply, fake_bot_api

CHAT = "5150"
CAPS = capabilities_for("telegram")


class Identity:
    """The one attribute ``send`` reads off L3-A's ContactChannelIdentity."""

    def __init__(self, chat_id: str = CHAT) -> None:
        self.platform_user_id = chat_id


def rendered(message: OutboundMessage) -> list[tuple[str, dict[str, Any]]]:
    """What the adapter would send: downgrade first, then build."""
    calls: list[tuple[str, dict[str, Any]]] = []
    for part in downgrade(message, CAPS).messages:
        calls.extend(wire_calls(CHAT, part))
    return calls


class TestTextAndMedia:
    def test_plain_text(self) -> None:
        assert rendered(OutboundMessage(blocks=(TextBlock(text="Hello"),))) == [
            ("sendMessage", {"chat_id": CHAT, "text": "Hello"})
        ]

    @pytest.mark.parametrize(
        ("kind", "method", "key"),
        [
            ("image", "sendPhoto", "photo"),
            ("audio", "sendAudio", "audio"),
            ("video", "sendVideo", "video"),
            ("file", "sendDocument", "document"),
        ],
    )
    def test_each_media_kind_has_its_own_method(self, kind: str, method: str, key: str) -> None:
        block = MediaBlock(kind=kind, url="https://cdn.example/a", caption="Look")
        assert rendered(OutboundMessage(blocks=(block,))) == [
            (method, {"chat_id": CHAT, key: "https://cdn.example/a", "caption": "Look"})
        ]

    def test_media_with_no_caption_omits_the_key(self) -> None:
        block = MediaBlock(kind="image", url="AgACfileid")
        # A file_id and a URL go in the same field — Telegram accepts either.
        assert rendered(OutboundMessage(blocks=(block,))) == [("sendPhoto", {"chat_id": CHAT, "photo": "AgACfileid"})]

    def test_an_over_long_caption_follows_as_its_own_message(self) -> None:
        """1024 against 4096: the downgrade renderer only knows the second, so
        the caption cap is the adapter's, and it spills rather than truncates."""
        caption = "c" * (MAX_CAPTION_CHARS + 50)
        calls = rendered(OutboundMessage(blocks=(MediaBlock(kind="image", url="u", caption=caption),)))
        assert [method for method, _ in calls] == ["sendPhoto", "sendMessage"]
        assert "caption" not in calls[0][1]
        # All of it, not the first 1024 characters.
        assert calls[1][1]["text"] == caption

    def test_text_over_4096_is_split_by_the_shared_renderer(self) -> None:
        calls = rendered(OutboundMessage(blocks=(TextBlock(text="w " * 4_000),)))
        assert len(calls) > 1
        assert all(method == "sendMessage" for method, _ in calls)
        assert all(len(payload["text"]) <= MAX_TEXT_CHARS for _, payload in calls)

    def test_a_blank_block_produces_no_call(self) -> None:
        # Telegram rejects an empty `text` outright, so this must not become a
        # message that fails at the platform.
        assert wire_calls(CHAT, OutboundMessage(blocks=(TextBlock(text="   "),))) == []


class TestKeyboards:
    def test_postback_buttons_become_an_inline_keyboard(self) -> None:
        message = OutboundMessage(
            blocks=(TextBlock(text="Pick one"),),
            buttons=(Button(id="yes", label="Yes"), Button(id="no", label="No")),
            node_id="ask",
        )
        (_method, payload) = rendered(message)[0]
        assert payload["reply_markup"] == {
            "inline_keyboard": [
                [{"text": "Yes", "callback_data": "ask:yes"}],
                [{"text": "No", "callback_data": "ask:no"}],
            ]
        }

    def test_url_buttons_carry_a_url_not_a_callback(self) -> None:
        message = OutboundMessage(
            blocks=(TextBlock(text="Read this"),),
            buttons=(Button(id="docs", label="Docs", url="https://example.test/docs"),),
            node_id="n1",
        )
        (_method, payload) = rendered(message)[0]
        assert payload["reply_markup"]["inline_keyboard"] == [[{"text": "Docs", "url": "https://example.test/docs"}]]

    def test_quick_replies_alone_become_a_one_time_reply_keyboard(self) -> None:
        message = OutboundMessage(
            blocks=(TextBlock(text="How was it?"),),
            quick_replies=(QuickReply(id="good", label="Good"), QuickReply(id="bad", label="Bad")),
        )
        (_method, payload) = rendered(message)[0]
        assert payload["reply_markup"] == {
            "keyboard": [[{"text": "Good"}], [{"text": "Bad"}]],
            "one_time_keyboard": True,
            "resize_keyboard": True,
        }

    def test_buttons_and_quick_replies_share_one_inline_keyboard(self) -> None:
        """Telegram allows one reply_markup per message. A QuickReply comes back
        as `button_id` exactly like a postback button, so folding them in keeps
        the semantics instead of dropping half the message's interaction."""
        message = OutboundMessage(
            blocks=(TextBlock(text="Pick"),),
            buttons=(Button(id="a", label="A"),),
            quick_replies=(QuickReply(id="b", label="B"),),
            node_id="n",
        )
        (_method, payload) = rendered(message)[0]
        assert payload["reply_markup"]["inline_keyboard"] == [
            [{"text": "A", "callback_data": "n:a"}],
            [{"text": "B", "callback_data": "n:b"}],
        ]
        assert "keyboard" not in payload["reply_markup"]

    def test_the_keyboard_rides_on_the_last_message(self) -> None:
        """Not on an image the rest of the message then scrolls past."""
        message = OutboundMessage(
            blocks=(MediaBlock(kind="image", url="u"), TextBlock(text="Pick")),
            buttons=(Button(id="a", label="A"),),
            node_id="n",
        )
        calls = rendered(message)
        assert "reply_markup" not in calls[0][1]
        assert "reply_markup" in calls[-1][1]

    def test_a_button_with_no_label_falls_back_to_its_id(self) -> None:
        message = OutboundMessage(blocks=(TextBlock(text="x"),), buttons=(Button(id="only-id", label=""),))
        (_method, payload) = rendered(message)[0]
        assert payload["reply_markup"]["inline_keyboard"][0][0]["text"] == "only-id"


class TestCallbackData:
    def test_a_long_pair_drops_the_node_prefix_and_keeps_the_button(self) -> None:
        """SPEC §6.2 asks for node_id:button_id and Telegram caps the field at 64
        bytes. The button id is the half the engine matches on, so it is the half
        that survives."""
        # 64 is the cap, and "<node>:yes" has to land over it for the fallback
        # to be the thing under test rather than an accident of length.
        node = "n" * (MAX_CALLBACK_DATA_BYTES - len(":yes") + 1)
        message = OutboundMessage(blocks=(TextBlock(text="x"),), buttons=(Button(id="yes", label="Y"),), node_id=node)
        (_method, payload) = rendered(message)[0]
        data = payload["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        # The separator survives even with nothing in front of it, so the
        # decoding stays unambiguous — see _callback_data.
        assert data == ":yes"
        assert len(data.encode()) <= MAX_CALLBACK_DATA_BYTES

    def test_a_button_id_that_cannot_fit_at_all_is_left_out(self) -> None:
        """The graph schema bounds ids to 64 ASCII characters, so this is only
        reachable from a hand-built OutboundMessage — but a keyboard entry that
        Telegram would reject must not go on the wire."""
        message = OutboundMessage(blocks=(TextBlock(text="x"),), buttons=(Button(id="é" * 60, label="Y"),))
        (_method, payload) = rendered(message)[0]
        assert "reply_markup" not in payload

    def test_no_node_id_still_produces_a_usable_button(self) -> None:
        """An agent reply or an API send has no node behind it."""
        message = OutboundMessage(blocks=(TextBlock(text="x"),), buttons=(Button(id="yes", label="Y"),))
        (_method, payload) = rendered(message)[0]
        assert payload["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == ":yes"

    def test_a_button_id_containing_a_colon_round_trips(self) -> None:
        """The graph schema forbids a colon in an id; a hand-built message from
        the inbox or the public API is not bound by it. Emitting the separator
        unconditionally is what keeps `a:b` from coming back as `b`."""
        message = OutboundMessage(blocks=(TextBlock(text="x"),), buttons=(Button(id="a:b", label="Y"),))
        (_method, payload) = rendered(message)[0]
        data = payload["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        assert data == ":a:b"
        assert _button_id(data) == "a:b"


class TestDowngrades:
    def test_a_gallery_becomes_one_message_per_card(self) -> None:
        gallery = GalleryBlock(
            cards=(
                Card(title="One", subtitle="First", image_url="https://cdn.example/1"),
                Card(title="Two", subtitle="Second", image_url="https://cdn.example/2"),
            )
        )
        calls = rendered(OutboundMessage(blocks=(gallery,)))
        assert [method for method, _ in calls] == ["sendPhoto", "sendMessage", "sendPhoto", "sendMessage"]
        assert calls[1][1]["text"] == "One\nFirst"
        assert calls[3][1]["text"] == "Two\nSecond"

    def test_a_card_becomes_image_plus_text(self) -> None:
        card = CardBlock(card=Card(title="T", subtitle="S", image_url="https://cdn.example/i", url="https://e.test"))
        calls = rendered(OutboundMessage(blocks=(card,)))
        assert calls[0] == ("sendPhoto", {"chat_id": CHAT, "photo": "https://cdn.example/i"})
        assert calls[1][1]["text"] == "T\nS\nhttps://e.test"

    def test_buttons_over_the_limit_become_numbered_options(self) -> None:
        buttons = tuple(Button(id=f"b{index}", label=f"Option {index}") for index in range(CAPS.max_buttons + 3))
        calls = rendered(OutboundMessage(blocks=(TextBlock(text="Pick"),), buttons=buttons, node_id="n"))
        (_method, payload) = calls[-1]
        assert len(payload["reply_markup"]["inline_keyboard"]) == CAPS.max_buttons
        # The overflow is not lost — it is in the text, numbered, so a contact
        # can still choose (SPEC §6.1).
        assert "Reply 1 for Option 10" in payload["text"]

    def test_a_card_gallery_with_buttons_keeps_each_cards_buttons_on_its_own_message(self) -> None:
        gallery = GalleryBlock(
            cards=(
                Card(title="One", buttons=(Button(id="one", label="Pick one"),)),
                Card(title="Two", buttons=(Button(id="two", label="Pick two"),)),
            )
        )
        calls = rendered(OutboundMessage(blocks=(gallery,), node_id="n"))
        keyboards = [payload.get("reply_markup", {}).get("inline_keyboard") for _m, payload in calls]
        assert keyboards == [
            [[{"text": "Pick one", "callback_data": "n:one"}]],
            [[{"text": "Pick two", "callback_data": "n:two"}]],
        ]


@pytest.mark.django_db
class TestSend:
    @pytest.fixture
    def telegram_connection(self, connection: ChannelConnection) -> ChannelConnection:
        store_bot_token(connection, BOT_TOKEN)
        connection.save(update_fields=["credentials", "updated_at"])
        return connection

    def test_a_send_reports_the_last_provider_message_id(self, telegram_connection: ChannelConnection) -> None:
        """One abstract message can be several sends; the id that matters is the
        one the contact is looking at."""
        with fake_bot_api() as fake:
            fake.reply("sendPhoto", Reply(result={"message_id": 10}))
            fake.reply("sendMessage", Reply(result={"message_id": 11}))
            result = TelegramAdapter().send(
                telegram_connection,
                Identity(),
                OutboundMessage(blocks=(MediaBlock(kind="image", url="u"), TextBlock(text="hi"))),
            )
        assert result.status == SendStatus.SENT
        assert result.provider_message_id == "11"
        assert fake.methods() == ["sendPhoto", "sendMessage"]

    def test_an_identity_with_no_chat_id_fails_rather_than_calling(
        self, telegram_connection: ChannelConnection
    ) -> None:
        with fake_bot_api() as fake:
            result = TelegramAdapter().send(
                telegram_connection, Identity(""), OutboundMessage(blocks=(TextBlock(text="hi"),))
            )
        assert result.status == SendStatus.FAILED
        assert result.error == "no_chat_id"
        assert fake.calls == []

    def test_a_message_with_nothing_sendable_is_reported_not_swallowed(
        self, telegram_connection: ChannelConnection
    ) -> None:
        with fake_bot_api() as fake:
            result = TelegramAdapter().send(telegram_connection, Identity(), OutboundMessage())
        assert result.status == SendStatus.FAILED
        assert result.error == "empty_message"
        assert fake.calls == []

    def test_429_becomes_a_rate_limit_error_carrying_retry_after(self, telegram_connection: ChannelConnection) -> None:
        """SPEC §6.2: "on HTTP 429 honor retry_after and reschedule". Telegram
        puts it in the body; the send pipeline reads it off this exception."""
        body = {
            "ok": False,
            "error_code": 429,
            "description": "Too Many Requests: retry after 37",
            "parameters": {"retry_after": 37},
        }
        with (
            fake_bot_api(lambda fake: fake.reply("sendMessage", Reply(status=429, body=body))),
            pytest.raises(RateLimitError) as caught,
        ):
            TelegramAdapter().send(telegram_connection, Identity(), OutboundMessage(blocks=(TextBlock(text="hi"),)))
        assert caught.value.retry_after == 37

    def test_the_retry_after_header_is_honoured_too(self, telegram_connection: ChannelConnection) -> None:
        reply = Reply(status=429, body={"ok": False, "error_code": 429}, headers={"Retry-After": "12"})
        with (
            fake_bot_api(lambda fake: fake.reply("sendMessage", reply)),
            pytest.raises(RateLimitError) as caught,
        ):
            TelegramAdapter().send(telegram_connection, Identity(), OutboundMessage(blocks=(TextBlock(text="hi"),)))
        assert caught.value.retry_after == 12

    def test_a_400_is_a_plain_api_error(self, telegram_connection: ChannelConnection) -> None:
        with (
            fake_bot_api(lambda fake: fake.reply("sendMessage", Reply(status=400))),
            pytest.raises(APIError) as caught,
        ):
            TelegramAdapter().send(telegram_connection, Identity(), OutboundMessage(blocks=(TextBlock(text="hi"),)))
        assert not isinstance(caught.value, RateLimitError)
        assert caught.value.status_code == 400

    def test_a_200_that_says_not_ok_is_still_an_error(self, telegram_connection: ChannelConnection) -> None:
        reply = Reply(status=200, body={"ok": False, "error_code": 111, "description": "nope"})
        with fake_bot_api(lambda fake: fake.reply("sendMessage", reply)), pytest.raises(APIError):
            TelegramAdapter().send(telegram_connection, Identity(), OutboundMessage(blocks=(TextBlock(text="hi"),)))

    def test_send_typing_uses_send_chat_action(self, telegram_connection: ChannelConnection) -> None:
        with fake_bot_api() as fake:
            TelegramAdapter().send_typing(telegram_connection, Identity())
        assert fake.payloads("sendChatAction") == [{"chat_id": CHAT, "action": "typing"}]

    def test_a_failed_typing_indicator_is_swallowed(self, telegram_connection: ChannelConnection) -> None:
        with fake_bot_api(lambda fake: fake.reply("sendChatAction", Reply(status=400))):
            TelegramAdapter().send_typing(telegram_connection, Identity())

    def test_a_connection_with_no_token_fails_before_any_call(self, telegram_connection: ChannelConnection) -> None:
        store_bot_token(telegram_connection, "")
        telegram_connection.save(update_fields=["credentials", "updated_at"])
        with fake_bot_api() as fake, pytest.raises(APIError):
            TelegramAdapter().send(telegram_connection, Identity(), OutboundMessage(blocks=(TextBlock(text="hi"),)))
        assert fake.calls == []
