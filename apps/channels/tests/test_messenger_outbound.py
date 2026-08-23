"""What actually goes on the wire, for every block and button combination.

``wire_calls`` is pure, so most of this is a table: an ``OutboundMessage`` in, the
exact Send API bodies Meta would receive out. That is worth having as a snapshot
rather than as prose, because the failure mode of a wrong payload is silent — a
message the platform rejects at send time, in production, on somebody else's page.

The downgrade cases go through :func:`~apps.channels.downgrade.downgrade` first,
which is what the adapter does, so they pin the *combination* of the shared
renderer and this adapter rather than either alone.

The message-tag half is deliberately driven from the registry rather than from
literals: SPEC §8's rules are ``apps.channels.policy``'s data, and a test that
restated them would pass while the policy said something else.
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
from apps.channels.models import ChannelConnection, ConnectionStatus
from apps.channels.providers.exceptions import APIError, RateLimitError
from apps.channels.providers.messenger import (
    ALLOWED_TAGS,
    MAX_BUTTON_TEMPLATE_TEXT_CHARS,
    MAX_BUTTON_TITLE_CHARS,
    MAX_PAYLOAD_BYTES,
    MAX_TEMPLATE_ELEMENTS,
    MAX_TEXT_CHARS,
    MessengerAdapter,
    _button_id,
    wire_calls,
)
from apps.channels.tests.messenger_support import PAGE_TOKEN, PSID, Reply, fake_graph
from apps.common.platforms import Platform

pytestmark = pytest.mark.django_db

CAPS = capabilities_for(Platform.MESSENGER.value)
RECIPIENT = {"id": PSID}


class Identity:
    """The attributes ``send`` reads off L3-A's ContactChannelIdentity."""

    def __init__(self, psid: str = PSID, contact: Any = None) -> None:
        self.platform_user_id = psid
        self.contact = contact


def rendered(message: OutboundMessage, *, tag: str | None = None) -> list[dict[str, Any]]:
    """What the adapter would send: downgrade first, then build."""
    calls: list[dict[str, Any]] = []
    for part in downgrade(message, CAPS).messages:
        calls.extend(wire_calls(RECIPIENT, part, tag=tag))
    return calls


def bodies(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [call["message"] for call in calls]


class TestTextAndMedia:
    def test_plain_text(self) -> None:
        assert rendered(OutboundMessage(blocks=(TextBlock(text="Hello"),))) == [
            {"recipient": RECIPIENT, "messaging_type": "RESPONSE", "message": {"text": "Hello"}}
        ]

    def test_an_empty_block_sends_nothing(self) -> None:
        """Meta rejects an empty ``text`` outright."""
        assert rendered(OutboundMessage(blocks=(TextBlock(text="   "),))) == []

    def test_long_text_is_split_at_the_platform_cap(self) -> None:
        message = OutboundMessage(blocks=(TextBlock(text="x" * (MAX_TEXT_CHARS + 50)),))
        parts = bodies(rendered(message))
        assert len(parts) == 2
        assert all(len(part["text"]) <= MAX_TEXT_CHARS for part in parts)

    @pytest.mark.parametrize("kind", ["image", "audio", "video", "file"])
    def test_media_becomes_an_attachment(self, kind: str) -> None:
        message = OutboundMessage(blocks=(MediaBlock(kind=kind, url="https://cdn.test/a"),))
        assert bodies(rendered(message)) == [{"attachment": {"type": kind, "payload": {"url": "https://cdn.test/a"}}}]

    def test_a_caption_travels_as_its_own_message(self) -> None:
        """Meta's attachment payload has no caption field, unlike Telegram's.

        Sending it afterwards keeps all of it, in order, which is what a person
        would do — and is better than dropping the words an author wrote.
        """
        message = OutboundMessage(blocks=(MediaBlock(kind="image", url="https://cdn.test/a", caption="Look"),))
        assert bodies(rendered(message)) == [
            {"attachment": {"type": "image", "payload": {"url": "https://cdn.test/a"}}},
            {"text": "Look"},
        ]

    def test_media_with_no_url_falls_back_to_text(self) -> None:
        message = OutboundMessage(blocks=(MediaBlock(kind="image", url="", caption="Look"),))
        assert bodies(rendered(message)) == [{"text": "Look"}]

    def test_is_reusable_is_never_set(self) -> None:
        """It makes Meta keep a copy of every asset we send. See ``_media_messages``."""
        message = OutboundMessage(blocks=(MediaBlock(kind="image", url="https://cdn.test/a"),))
        payload = bodies(rendered(message))[0]["attachment"]["payload"]
        assert "is_reusable" not in payload


class TestCardsAndGalleries:
    def test_a_card_is_a_generic_template(self) -> None:
        card = Card(title="Sneaker", subtitle="Blue", image_url="https://cdn.test/s.jpg", url="https://shop.test/s")
        (body,) = bodies(rendered(OutboundMessage(blocks=(CardBlock(card=card),))))
        payload = body["attachment"]["payload"]
        assert payload["template_type"] == "generic"
        assert payload["elements"] == [
            {
                "title": "Sneaker",
                "subtitle": "Blue",
                "image_url": "https://cdn.test/s.jpg",
                "default_action": {"type": "web_url", "url": "https://shop.test/s"},
            }
        ]

    def test_a_gallery_is_one_template_with_many_elements(self) -> None:
        cards = tuple(Card(title=f"Card {index}") for index in range(3))
        (body,) = bodies(rendered(OutboundMessage(blocks=(GalleryBlock(cards=cards),))))
        assert len(body["attachment"]["payload"]["elements"]) == 3

    def test_a_long_gallery_is_split_rather_than_cut(self) -> None:
        """Meta caps a carousel at ten elements. Losing the eleventh card silently
        would be worse than sending a second carousel."""
        cards = tuple(Card(title=f"Card {index}") for index in range(MAX_TEMPLATE_ELEMENTS + 3))
        parts = bodies(rendered(OutboundMessage(blocks=(GalleryBlock(cards=cards),))))
        assert [len(part["attachment"]["payload"]["elements"]) for part in parts] == [MAX_TEMPLATE_ELEMENTS, 3]

    def test_a_card_with_no_title_falls_back_then_drops(self) -> None:
        """Meta rejects the whole template over one titleless element."""
        with_subtitle = Card(subtitle="Only a subtitle")
        (body,) = bodies(rendered(OutboundMessage(blocks=(CardBlock(card=with_subtitle),))))
        assert body["attachment"]["payload"]["elements"][0]["title"] == "Only a subtitle"

        assert rendered(OutboundMessage(blocks=(CardBlock(card=Card()),))) == []


class TestButtons:
    def test_buttons_turn_the_last_text_into_a_button_template(self) -> None:
        message = OutboundMessage(
            blocks=(TextBlock(text="Pick one"),),
            buttons=(Button(id="a", label="Alpha"), Button(id="b", label="Beta", url="https://x.test")),
            node_id="node-1",
        )
        (body,) = bodies(rendered(message))
        payload = body["attachment"]["payload"]
        assert payload["template_type"] == "button"
        assert payload["text"] == "Pick one"
        assert payload["buttons"] == [
            {"type": "postback", "title": "Alpha", "payload": "node-1:a"},
            {"type": "web_url", "url": "https://x.test", "title": "Beta"},
        ]

    def test_the_downgrade_renderer_caps_the_count_before_we_see_them(self) -> None:
        """Three is Meta's limit and the capability table's. The renderer applies it."""
        message = OutboundMessage(
            blocks=(TextBlock(text="Pick"),),
            buttons=tuple(Button(id=str(index), label=f"B{index}") for index in range(7)),
        )
        (body,) = bodies(rendered(message))
        assert len(body["attachment"]["payload"]["buttons"]) == CAPS.max_buttons

    def test_over_long_text_keeps_the_tail_with_the_buttons(self) -> None:
        """A button template's text caps at 640 against 2000 for a plain message."""
        message = OutboundMessage(
            blocks=(TextBlock(text="y" * (MAX_BUTTON_TEMPLATE_TEXT_CHARS + 100)),),
            buttons=(Button(id="a", label="Alpha"),),
        )
        parts = bodies(rendered(message))
        assert parts[0]["text"] == "y" * 100
        assert parts[-1]["attachment"]["payload"]["text"] == "y" * MAX_BUTTON_TEMPLATE_TEXT_CHARS

    def test_buttons_after_an_attachment_become_quick_replies(self) -> None:
        """Meta has no ``reply_markup``: buttons ride inside a template.

        A postback quick reply comes back as ``EventPayload.button_id`` exactly
        like a postback button does, so the semantics survive the change of
        clothes — the trade ``telegram._reply_markup`` makes in reverse.
        """
        message = OutboundMessage(
            blocks=(MediaBlock(kind="image", url="https://cdn.test/a"),),
            buttons=(Button(id="a", label="Alpha"),),
            node_id="node-2",
        )
        (body,) = bodies(rendered(message))
        assert body["quick_replies"] == [{"content_type": "text", "title": "Alpha", "payload": "node-2:a"}]

    def test_a_url_button_after_an_attachment_is_dropped_rather_than_faked(self) -> None:
        """A quick reply cannot open a link, and a chip that does nothing is worse."""
        message = OutboundMessage(
            blocks=(MediaBlock(kind="image", url="https://cdn.test/a"),),
            buttons=(Button(id="a", label="Alpha", url="https://x.test"),),
        )
        (body,) = bodies(rendered(message))
        assert "quick_replies" not in body

    def test_a_button_title_is_bounded(self) -> None:
        message = OutboundMessage(
            blocks=(TextBlock(text="Pick"),),
            buttons=(Button(id="a", label="L" * 100),),
        )
        (body,) = bodies(rendered(message))
        assert len(body["attachment"]["payload"]["buttons"][0]["title"]) == MAX_BUTTON_TITLE_CHARS


class TestQuickReplies:
    def test_quick_replies_ride_on_the_last_message(self) -> None:
        message = OutboundMessage(
            blocks=(TextBlock(text="One"), TextBlock(text="Two")),
            quick_replies=(QuickReply(id="yes", label="Yes"),),
            node_id="node-3",
        )
        parts = bodies(rendered(message))
        assert "quick_replies" not in parts[0]
        assert parts[-1]["quick_replies"] == [{"content_type": "text", "title": "Yes", "payload": "node-3:yes"}]

    def test_the_count_is_capped_by_the_capability_table(self) -> None:
        message = OutboundMessage(
            blocks=(TextBlock(text="Pick"),),
            quick_replies=tuple(QuickReply(id=str(index), label=f"Q{index}") for index in range(30)),
        )
        (body,) = bodies(rendered(message))
        assert len(body["quick_replies"]) == CAPS.max_quick_replies


class TestPayloadEncoding:
    def test_a_payload_carries_the_node_and_the_button(self) -> None:
        message = OutboundMessage(
            blocks=(TextBlock(text="Pick"),), buttons=(Button(id="buy", label="Buy"),), node_id="node-9"
        )
        (body,) = bodies(rendered(message))
        assert body["attachment"]["payload"]["buttons"][0]["payload"] == "node-9:buy"

    def test_the_separator_is_present_even_with_no_node(self) -> None:
        """A button id may contain a colon; a node id never can.

        Without an unconditional separator, ``"a:b"`` on the wire would come back
        as ``"b"`` and match no handle.
        """
        message = OutboundMessage(blocks=(TextBlock(text="Pick"),), buttons=(Button(id="a:b", label="AB"),))
        (body,) = bodies(rendered(message))
        assert body["attachment"]["payload"]["buttons"][0]["payload"] == ":a:b"
        assert _button_id(":a:b") == "a:b"

    def test_an_over_long_payload_drops_the_node_prefix_first(self) -> None:
        """The node id is decoration; the button id has to survive."""
        long_button = "b" * (MAX_PAYLOAD_BYTES - 10)
        message = OutboundMessage(
            blocks=(TextBlock(text="Pick"),),
            buttons=(Button(id=long_button, label="B"),),
            node_id="n" * 100,
        )
        (body,) = bodies(rendered(message))
        assert body["attachment"]["payload"]["buttons"][0]["payload"] == f":{long_button}"

    def test_a_button_too_long_to_represent_is_left_out(self) -> None:
        message = OutboundMessage(
            blocks=(TextBlock(text="Pick"),), buttons=(Button(id="b" * (MAX_PAYLOAD_BYTES + 10), label="B"),)
        )
        (body,) = bodies(rendered(message))
        assert body == {"text": "Pick"}

    def test_meta_and_telegram_encode_a_press_the_same_way(self) -> None:
        """SPEC §6.2 fixes ``node_id:button_id``, and §6.4 takes the same shape."""
        from apps.channels.providers.telegram import _button_id as telegram_button_id

        assert _button_id("node-1:buy") == telegram_button_id("node-1:buy") == "buy"


class TestMessagingType:
    def test_no_tag_is_a_response(self) -> None:
        (call,) = rendered(OutboundMessage(blocks=(TextBlock(text="Hi"),)))
        assert call["messaging_type"] == "RESPONSE"
        assert "tag" not in call

    @pytest.mark.parametrize("tag", sorted(ALLOWED_TAGS))
    def test_a_tag_becomes_a_message_tag_send(self, tag: str) -> None:
        (call,) = rendered(OutboundMessage(blocks=(TextBlock(text="Hi"),)), tag=tag)
        assert call["messaging_type"] == "MESSAGE_TAG"
        assert call["tag"] == tag

    def test_the_allowed_tags_are_the_registrys_and_not_a_literal(self) -> None:
        """Contract 4: the adapter reads the capability table, never patches it."""
        assert frozenset(CAPS.tags_supported) == ALLOWED_TAGS

    def test_the_policys_non_promotional_tags_are_all_sendable(self) -> None:
        """SPEC §6.4's three tags come from the policy row; the wire accepts them."""
        from apps.channels.policy import NeedsTag, policy_for

        outside = policy_for(Platform.MESSENGER.value).outside_window
        assert isinstance(outside, NeedsTag)
        assert set(outside.tags) <= ALLOWED_TAGS


class TestSending:
    def test_a_send_posts_to_the_pages_messages_edge(self, page: ChannelConnection) -> None:
        with fake_graph() as graph:
            result = MessengerAdapter().send(page, Identity(), OutboundMessage(blocks=(TextBlock(text="Hi"),)))
        assert result.status == SendStatus.SENT
        assert result.provider_message_id == "mid.out-1"
        assert graph.paths() == [f"/v21.0/{page.external_id}/messages"]

    def test_the_token_travels_in_a_header_and_never_in_the_url(self, page: ChannelConnection) -> None:
        """SECURITY-BASELINE §5. ``httpx`` logs the URL of every request at INFO."""
        with fake_graph() as graph:
            MessengerAdapter().send(page, Identity(), OutboundMessage(blocks=(TextBlock(text="Hi"),)))
        (call,) = graph.calls
        assert call.authorization == f"Bearer {PAGE_TOKEN}"
        assert PAGE_TOKEN not in call.path
        assert PAGE_TOKEN not in "".join(f"{key}={value}" for key, value in call.params.items())

    def test_no_recipient_fails_without_calling_anyone(self, page: ChannelConnection) -> None:
        with fake_graph() as graph:
            result = MessengerAdapter().send(page, Identity(psid=""), OutboundMessage(blocks=(TextBlock(text="Hi"),)))
        assert result.status == SendStatus.FAILED
        assert result.error == "no_recipient"
        assert graph.calls == []

    def test_a_message_with_nothing_sendable_is_reported_rather_than_counted_as_sent(
        self, page: ChannelConnection
    ) -> None:
        with fake_graph() as graph:
            result = MessengerAdapter().send(page, Identity(), OutboundMessage())
        assert result.status == SendStatus.FAILED
        assert result.error == "empty_message"
        assert graph.calls == []

    def test_a_tag_this_platform_does_not_accept_refuses_the_send(self, page: ChannelConnection) -> None:
        """Unreachable through the compliance engine, and refused anyway.

        Meta restricts pages over a message sent under a tag it did not accept, so
        a bug upstream has to fail visibly here rather than reach the platform.
        """
        message = OutboundMessage(blocks=(TextBlock(text="Hi"),), tag="PROMOTIONAL_BLAST")
        with fake_graph() as graph:
            result = MessengerAdapter().send(page, Identity(), message)
        assert result.status == SendStatus.FAILED
        assert result.error == "unsupported_tag"
        assert graph.calls == []

    def test_the_last_provider_id_is_the_one_reported(self, page: ChannelConnection) -> None:
        """One message can be several sends; a receipt references the last bubble.

        A captioned image is two Send API calls, and the one the person is looking
        at — the one a delivery receipt will name — is the second.
        """

        def configure(graph: Any) -> None:
            graph.default = Reply(body={"message_id": "mid.b"})

        message = OutboundMessage(blocks=(MediaBlock(kind="image", url="https://cdn.test/a", caption="Look"),))
        with fake_graph(configure) as graph:
            result = MessengerAdapter().send(page, Identity(), message)
        assert len(graph.calls) == 2
        assert result.provider_message_id == "mid.b"

    def test_a_429_becomes_a_rate_limit_error_the_pipeline_understands(self, page: ChannelConnection) -> None:
        def configure(graph: Any) -> None:
            graph.reply("/messages", Reply(status=429, headers={"Retry-After": "12"}))

        with fake_graph(configure), pytest.raises(RateLimitError) as caught:
            MessengerAdapter().send(page, Identity(), OutboundMessage(blocks=(TextBlock(text="Hi"),)))
        assert caught.value.retry_after == 12


class TestErrorsThatMeanSomethingDurable:
    def test_a_dead_token_parks_the_connection_and_notifies(self, page: ChannelConnection) -> None:
        """Meta's 190. Every further send is a wasted call until it is reconnected."""
        from apps.notifications.models import Notification

        def configure(graph: Any) -> None:
            graph.reply("/messages", Reply(status=400, body={"error": {"message": "bad", "code": 190}}))

        with fake_graph(configure), pytest.raises(APIError):
            MessengerAdapter().send(page, Identity(), OutboundMessage(blocks=(TextBlock(text="Hi"),)))

        page.refresh_from_db()
        assert page.status == ConnectionStatus.NEEDS_REAUTH
        assert Notification.objects.filter(event_type="channel_needs_reauth").exists()

    def test_parking_is_idempotent(self, page: ChannelConnection) -> None:
        """Ten failed sends in a minute must not be ten rounds of notifications.

        Counted as rows rather than as calls because ``notify`` fans out to every
        workspace admin, so "one notification" is however many admins there are —
        the property under test is that the *second* failure adds none.
        """
        from apps.notifications.models import Notification

        def configure(graph: Any) -> None:
            graph.reply("/messages", Reply(status=400, body={"error": {"message": "bad", "code": 190}}))

        def fail_once() -> None:
            with fake_graph(configure), pytest.raises(APIError):
                MessengerAdapter().send(page, Identity(), OutboundMessage(blocks=(TextBlock(text="Hi"),)))
            page.refresh_from_db()

        fail_once()
        after_first = Notification.objects.filter(event_type="channel_needs_reauth").count()
        assert after_first > 0

        fail_once()
        fail_once()
        assert Notification.objects.filter(event_type="channel_needs_reauth").count() == after_first

    def test_an_unreachable_person_records_an_opt_out_through_ingest(self, page: ChannelConnection) -> None:
        """Meta's 551. The adapter never writes ``opted_out_at`` itself.

        ROADMAP contract 3 gives that column one write site, and
        ``apps/messaging/tests/test_write_sites.py`` scans the AST for a second.
        So the adapter raises the event the pipeline already knows how to apply.
        """
        from apps.channels.events import EventType

        seen: list[Any] = []

        def configure(graph: Any) -> None:
            graph.reply("/messages", Reply(status=400, body={"error": {"message": "gone", "code": 551}}))

        from apps.channels import ingest as channels_ingest

        channels_ingest.register_processor(lambda connection, events: seen.extend(events), name="test-capture")
        try:
            with fake_graph(configure), pytest.raises(APIError):
                MessengerAdapter().send(page, Identity(), OutboundMessage(blocks=(TextBlock(text="Hi"),)))
        finally:
            channels_ingest.unregister_processor("test-capture")

        assert [event.type for event in seen] == [EventType.OPT_OUT]
        assert seen[0].platform_user_id == PSID
        page.refresh_from_db()
        assert page.status == ConnectionStatus.ACTIVE

    def test_an_ordinary_failure_changes_nothing_about_the_connection(self, page: ChannelConnection) -> None:
        def configure(graph: Any) -> None:
            graph.reply("/messages", Reply(status=500))

        with fake_graph(configure), pytest.raises(APIError):
            MessengerAdapter().send(page, Identity(), OutboundMessage(blocks=(TextBlock(text="Hi"),)))
        page.refresh_from_db()
        assert page.status == ConnectionStatus.ACTIVE


class TestParkingIsTransactionSafe:
    def test_a_failed_park_does_not_poison_the_surrounding_transaction(
        self, page: ChannelConnection, monkeypatch: Any
    ) -> None:
        """``mark_needs_reauth`` runs from ``send``, which the routing pipeline
        calls inside ``transaction.atomic()`` while holding the contact lock.

        Catching a database error there without a savepoint leaves the transaction
        marked aborted, so every later query in it fails with "current transaction
        is aborted" — the message row could not be finalised and the whole event
        would be lost rather than one send failing.
        """
        from django.db import IntegrityError, transaction

        from apps.channels.providers import messenger as messenger_module

        def refuse(*args: Any, **kwargs: Any) -> None:
            raise IntegrityError("nope")

        with transaction.atomic():
            monkeypatch.setattr(type(page), "save", refuse)
            messenger_module.mark_needs_reauth(page, platform_label="Facebook Messenger")
            monkeypatch.undo()
            # The proof: the transaction is still usable.
            assert ChannelConnection.objects.unscoped().filter(pk=page.pk).exists()


class TestCourtesies:
    @pytest.mark.parametrize(
        ("method", "action"),
        [("send_typing", "typing_on"), ("mark_seen", "mark_seen")],
    )
    def test_sender_actions(self, method: str, action: str, page: ChannelConnection) -> None:
        with fake_graph() as graph:
            getattr(MessengerAdapter(), method)(page, Identity())
        assert graph.bodies("/messages") == [{"recipient": RECIPIENT, "sender_action": action}]

    def test_a_failed_courtesy_is_swallowed(self, page: ChannelConnection) -> None:
        """Cosmetic. A person's reply must not be lost because a typing dot was."""

        def configure(graph: Any) -> None:
            graph.reply("/messages", Reply(status=500))

        with fake_graph(configure):
            MessengerAdapter().send_typing(page, Identity())
            MessengerAdapter().mark_seen(page, Identity())


class TestLifecycle:
    def test_disconnect_unsubscribes_the_page(self, page: ChannelConnection) -> None:
        with fake_graph() as graph:
            MessengerAdapter().on_disconnect(page)
        assert graph.calls[0].method == "DELETE"
        assert graph.paths() == [f"/v21.0/{page.external_id}/subscribed_apps"]

    def test_rotating_the_webhook_secret_calls_nobody(self, page: ChannelConnection) -> None:
        """Meta signs with the app secret; there is no per-page secret to push."""
        with fake_graph() as graph:
            MessengerAdapter().on_webhook_secret_rotated(page, "a-new-secret")
        assert graph.calls == []
