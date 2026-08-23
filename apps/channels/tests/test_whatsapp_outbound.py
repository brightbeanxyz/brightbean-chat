"""The wire payloads a WhatsApp send produces (SPEC §6.5).

``wire_calls`` is pure — no HTTP, no database, no clock — which is what lets
this file be a table of payloads checked against Meta's documentation rather
than a set of assertions about a send loop.

Two properties every case here is really about:

* the **shared downgrade renderer** decides what survives, not this adapter.
  Buttons over three, URL buttons, unsupported media: all of those are already
  text by the time ``wire_calls`` runs, because of numbers declared in
  ``apps.channels.capabilities`` (ROADMAP contract 4);
* what is left is genuinely WhatsApp's — which of the two interactive shapes to
  use, the 1024-character interactive body against a 4096-character text body,
  and how ``template_variables`` becomes ``components``.
"""

import json
from typing import Any

import httpx
import pytest

from apps.channels.capabilities import capabilities_for
from apps.channels.downgrade import downgrade
from apps.channels.events import (
    Button,
    MediaBlock,
    OutboundMessage,
    QuickReply,
    SendStatus,
    TextBlock,
)
from apps.channels.providers.exceptions import APIError, RateLimitError
from apps.channels.providers.whatsapp import (
    INTERACTIVE_BODY_FALLBACK,
    MAX_INTERACTIVE_BODY_CHARS,
    WhatsAppAdapter,
    wire_calls,
)
from apps.channels.tests.whatsapp_support import (
    ACCESS_TOKEN,
    PHONE_NUMBER_ID,
    PLATFORM_USER_ID,
    Reply,
    fake_graph_api,
    make_connection,
)
from apps.common.platforms import Platform

TO = PLATFORM_USER_ID


class _Identity:
    """The shape ``Adapter.send`` reads off L3-A's ContactChannelIdentity."""

    def __init__(self, address: str = TO) -> None:
        self.platform_user_id = address


def text(body: str) -> OutboundMessage:
    return OutboundMessage(blocks=(TextBlock(text=body),))


class TestSessionPayloads:
    def test_text(self) -> None:
        assert wire_calls(TO, text("Hello")) == [
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": TO,
                "type": "text",
                "text": {"body": "Hello", "preview_url": False},
            }
        ]

    def test_link_previews_stay_off(self) -> None:
        """Turning them on would make Meta fetch a URL a flow author supplied.

        That is a server-side fetch of an arbitrary address performed by somebody
        else's infrastructure, for content nobody reviewed.
        """
        (payload,) = wire_calls(TO, text("look at https://example.test"))
        assert payload["text"]["preview_url"] is False

    def test_an_empty_block_produces_no_call(self) -> None:
        """Meta rejects an empty body outright."""
        assert wire_calls(TO, text("   ")) == []

    def test_long_text_is_split_rather_than_truncated(self) -> None:
        payloads = wire_calls(TO, text("x" * 9000))
        assert len(payloads) == 3
        assert "".join(p["text"]["body"] for p in payloads).count("x") == 9000

    def test_image_with_a_caption_is_one_message(self) -> None:
        message = OutboundMessage(blocks=(MediaBlock(kind="image", url="https://x.test/a.png", caption="Here"),))
        assert wire_calls(TO, message) == [
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": TO,
                "type": "image",
                "image": {"link": "https://x.test/a.png", "caption": "Here"},
            }
        ]

    def test_a_document_uses_metas_own_name_for_the_kind(self) -> None:
        message = OutboundMessage(blocks=(MediaBlock(kind="file", url="https://x.test/a.pdf"),))
        (payload,) = wire_calls(TO, message)
        assert payload["type"] == "document"
        assert payload["document"] == {"link": "https://x.test/a.pdf"}

    def test_audio_carries_its_caption_as_a_second_message(self) -> None:
        """Meta gives audio no caption field, and dropping the words is not an option."""
        message = OutboundMessage(blocks=(MediaBlock(kind="audio", url="https://x.test/a.ogg", caption="Listen"),))
        first, second = wire_calls(TO, message)
        assert first["type"] == "audio" and "caption" not in first["audio"]
        assert second["text"]["body"] == "Listen"

    def test_an_over_long_caption_follows_as_its_own_message(self) -> None:
        caption = "y" * 2000
        message = OutboundMessage(blocks=(MediaBlock(kind="image", url="https://x.test/a.png", caption=caption),))
        first, second = wire_calls(TO, message)
        assert "caption" not in first["image"]
        assert second["text"]["body"].startswith("y")

    def test_a_media_block_with_no_url_degrades_to_its_caption(self) -> None:
        message = OutboundMessage(blocks=(MediaBlock(kind="image", url="", caption="No picture"),))
        (payload,) = wire_calls(TO, message)
        assert payload["type"] == "text"


class TestInteractiveShapes:
    def test_buttons_become_reply_buttons(self) -> None:
        message = OutboundMessage(
            blocks=(TextBlock(text="Pick one"),),
            buttons=(Button(id="yes", label="Yes"), Button(id="no", label="No")),
            node_id="n1",
        )
        (payload,) = wire_calls(TO, message)
        assert payload["type"] == "interactive"
        assert payload["interactive"]["type"] == "button"
        assert payload["interactive"]["body"] == {"text": "Pick one"}
        assert payload["interactive"]["action"]["buttons"] == [
            {"type": "reply", "reply": {"id": "yes", "title": "Yes"}},
            {"type": "reply", "reply": {"id": "no", "title": "No"}},
        ]

    def test_quick_replies_become_a_list(self) -> None:
        message = OutboundMessage(
            blocks=(TextBlock(text="Size?"),),
            quick_replies=tuple(QuickReply(id=f"q{i}", label=f"Option {i}") for i in range(4)),
        )
        (payload,) = wire_calls(TO, message)
        assert payload["interactive"]["type"] == "list"
        rows = payload["interactive"]["action"]["sections"][0]["rows"]
        assert [row["id"] for row in rows] == ["q0", "q1", "q2", "q3"]
        assert payload["interactive"]["action"]["button"]

    def test_buttons_win_when_a_message_has_both(self) -> None:
        """One interactive shape per message, and buttons are the visible one.

        A hand-built message's quick replies ride along as reply buttons rather
        than being dropped: a QuickReply comes back as EventPayload.button_id
        exactly like a postback button does, so the semantics survive the change
        of clothes. A *downgraded* one never gets here — see
        ``TestExclusiveInteraction``.
        """
        message = OutboundMessage(
            blocks=(TextBlock(text="Hi"),),
            buttons=(Button(id="a", label="A"),),
            quick_replies=(QuickReply(id="b", label="B"),),
        )
        (payload,) = wire_calls(TO, message)
        assert payload["interactive"]["type"] == "button"
        assert [b["reply"]["id"] for b in payload["interactive"]["action"]["buttons"]] == ["a", "b"]

    def test_an_over_long_id_is_refused_rather_than_trimmed(self) -> None:
        """A trimmed id is sent, tapped, and comes back matching no handle."""
        message = OutboundMessage(
            blocks=(TextBlock(text="Hi"),),
            buttons=(Button(id="x" * 300, label="A"), Button(id="ok", label="B")),
        )
        (payload,) = wire_calls(TO, message)
        assert [b["reply"]["id"] for b in payload["interactive"]["action"]["buttons"]] == ["ok"]

    def test_an_over_long_row_title_is_clipped_and_kept_in_the_description(self) -> None:
        """Meta rejects the whole message on an over-long title, so the choice is
        a clipped option or no message at all."""
        label = "A very long option label that will not fit in a list row title"
        message = OutboundMessage(blocks=(TextBlock(text="?"),), quick_replies=(QuickReply(id="q", label=label),))
        (payload,) = wire_calls(TO, message)
        row = payload["interactive"]["action"]["sections"][0]["rows"][0]
        assert len(row["title"]) <= 24
        assert row["description"].startswith("A very long")

    def test_a_url_button_never_becomes_a_reply_button(self) -> None:
        """It would come back matching no handle. ``url_buttons`` is False for
        this platform so the shared renderer inlines it; this is the backstop for
        a caller that skipped the downgrade."""
        message = OutboundMessage(
            blocks=(TextBlock(text="Hi"),),
            buttons=(Button(id="u", label="Open", url="https://x.test"),),
        )
        payloads = wire_calls(TO, message)
        assert all(p["type"] != "interactive" for p in payloads)

    def test_the_interactive_body_cap_splits_rather_than_truncates(self) -> None:
        """1024 for an interactive body against 4096 for a text one — a limit of
        this shape rather than of the platform, so the renderer does not know it."""
        message = OutboundMessage(blocks=(TextBlock(text="z" * 3000),), buttons=(Button(id="a", label="A"),))
        *leading, last = wire_calls(TO, message)
        assert leading and all(p["type"] == "text" for p in leading)
        assert last["type"] == "interactive"
        assert len(last["interactive"]["body"]["text"]) <= MAX_INTERACTIVE_BODY_CHARS

    def test_a_media_only_message_with_buttons_still_gets_a_body(self) -> None:
        """Meta requires one, and dropping the buttons would be worse."""
        message = OutboundMessage(
            blocks=(MediaBlock(kind="image", url="https://x.test/a.png"),),
            buttons=(Button(id="a", label="A"),),
        )
        first, second = wire_calls(TO, message)
        assert first["type"] == "image"
        assert second["interactive"]["body"]["text"] == INTERACTIVE_BODY_FALLBACK

    def test_text_before_media_keeps_its_own_bubble(self) -> None:
        """A keyboard belongs on the final bubble, not on something the rest of
        the message scrolls past — the call Telegram's _reply_markup makes too."""
        message = OutboundMessage(
            blocks=(TextBlock(text="Look:"), MediaBlock(kind="image", url="https://x.test/a.png")),
            buttons=(Button(id="a", label="A"),),
        )
        text, image, interactive = wire_calls(TO, message)
        assert text["text"]["body"] == "Look:"
        assert image["type"] == "image"
        assert interactive["interactive"]["body"]["text"] == INTERACTIVE_BODY_FALLBACK


class TestExclusiveInteraction:
    """Buttons and a list cannot share a message, and nothing may be dropped.

    WhatsApp's `interactive` message is a reply-button set *or* a list, never
    both, while the shared renderer fills ``max_buttons`` and
    ``max_quick_replies`` as two independent budgets. Declaring
    ``interaction_is_exclusive`` is what reconciles the two — without it the
    renderer handed over seven controls, the adapter could show three, and the
    other four reached the contact in no form at all: not buttons, not rows, and
    not numbered text, because the renderer believed they were native.
    """

    MESSAGE = OutboundMessage(
        blocks=(TextBlock(text="Pick"),),
        buttons=tuple(Button(id=f"b{i}", label=f"B{i}") for i in range(3)),
        quick_replies=tuple(QuickReply(id=f"q{i}", label=f"Q{i}") for i in range(4)),
    )

    def rendered(self) -> Any:
        return downgrade(self.MESSAGE, capabilities_for(Platform.WHATSAPP))

    def test_every_control_reaches_the_contact(self) -> None:
        result = self.rendered()
        payloads = [p for message in result.messages for p in wire_calls(TO, message)]
        wire = json.dumps(payloads)
        for index in range(3):
            assert f'"id": "b{index}"' in wire, f"button b{index} was dropped"
        for index in range(4):
            assert f"Q{index}" in wire, f"quick reply q{index} was dropped"

    def test_the_quick_replies_are_numbered_so_a_reply_can_match(self) -> None:
        """The mapping is what L4-A matches a numeric reply against; without it
        the contact's answer resolves to no handle and the node waits forever."""
        result = self.rendered()
        assert result.numeric_replies == {"1": "q0", "2": "q1", "3": "q2", "4": "q3"}

    def test_the_downgrade_says_what_it_did(self) -> None:
        (note,) = self.rendered().notes
        assert "cannot share a message with buttons" in note

    def test_quick_replies_alone_still_get_the_full_list(self) -> None:
        """The exclusivity bites only when buttons are present."""
        message = OutboundMessage(
            blocks=(TextBlock(text="Pick"),),
            quick_replies=tuple(QuickReply(id=f"q{i}", label=f"Q{i}") for i in range(10)),
        )
        result = downgrade(message, capabilities_for(Platform.WHATSAPP))
        assert result.numeric_replies == {}
        (payload,) = wire_calls(TO, result.messages[0])
        assert len(payload["interactive"]["action"]["sections"][0]["rows"]) == 10


@pytest.mark.django_db
class TestDowngradeDecidesWhatSurvives:
    """The numbers are in the capability table; this asserts the effect."""

    def test_a_fourth_button_arrives_as_a_numbered_option(self, tenancy: Any) -> None:
        message = OutboundMessage(
            blocks=(TextBlock(text="Pick"),),
            buttons=tuple(Button(id=f"b{i}", label=f"Button {i}") for i in range(5)),
        )
        with fake_graph_api() as fake:
            WhatsAppAdapter().send(make_connection(tenancy.workspace), _Identity(), message)
        (interactive,) = [p for p in fake.payloads("messages") if p["type"] == "interactive"]
        assert len(interactive["interactive"]["action"]["buttons"]) == 3
        # Numbering starts at 1 for the *overflow*: the three that fit are real
        # buttons and never get a number, so the first numbered option is the
        # fourth button.
        assert "Reply 1 for Button 3" in interactive["interactive"]["body"]["text"]
        assert "Reply 2 for Button 4" in interactive["interactive"]["body"]["text"]

    def test_a_url_button_is_inlined_as_a_link(self, tenancy: Any) -> None:
        message = OutboundMessage(
            blocks=(TextBlock(text="Hi"),),
            buttons=(Button(id="u", label="Open", url="https://x.test/order"),),
        )
        with fake_graph_api() as fake:
            WhatsAppAdapter().send(make_connection(tenancy.workspace), _Identity(), message)
        bodies = [p["text"]["body"] for p in fake.payloads("messages") if p["type"] == "text"]
        assert any("Open: https://x.test/order" in body for body in bodies)

    def test_an_eleventh_list_row_is_numbered_instead(self, tenancy: Any) -> None:
        message = OutboundMessage(
            blocks=(TextBlock(text="Pick"),),
            quick_replies=tuple(QuickReply(id=f"q{i}", label=f"Option {i}") for i in range(12)),
        )
        with fake_graph_api() as fake:
            WhatsAppAdapter().send(make_connection(tenancy.workspace), _Identity(), message)
        (interactive,) = [p for p in fake.payloads("messages") if p["type"] == "interactive"]
        assert len(interactive["interactive"]["action"]["sections"][0]["rows"]) == 10
        assert "Reply 1 for Option 10" in interactive["interactive"]["body"]["text"]


class TestTemplatePayloads:
    def test_a_template_send_carries_only_the_template(self) -> None:
        """A template *is* the message Meta approved. Sending the blocks beside it
        would put the same words on the wire twice, once outside the rules."""
        message = OutboundMessage(
            blocks=(TextBlock(text="Hi Ada, order 42 shipped."),),
            template_ref="order_shipped/en_US",
            template_variables=(("body.1", "Ada"), ("body.2", "42")),
        )
        assert wire_calls(TO, message) == [
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": TO,
                "type": "template",
                "template": {
                    "name": "order_shipped",
                    "language": {"code": "en_US"},
                    "components": [
                        {
                            "type": "body",
                            "parameters": [{"type": "text", "text": "Ada"}, {"type": "text", "text": "42"}],
                        }
                    ],
                },
            }
        ]

    def test_components_are_ordered_by_slot_number_not_arrival(self) -> None:
        """Meta positions parameters by index: {{2}} is the second parameter
        whether or not it was filled second."""
        message = OutboundMessage(
            template_ref="t/en",
            template_variables=(("body.2", "second"), ("body.1", "first")),
        )
        (payload,) = wire_calls(TO, message)
        assert [p["text"] for p in payload["template"]["components"][0]["parameters"]] == ["first", "second"]

    def test_header_body_and_button_become_three_components(self) -> None:
        message = OutboundMessage(
            template_ref="t/en_GB",
            template_variables=(("header.1", "March"), ("body.1", "Ada"), ("button.0.1", "orders/42")),
        )
        (payload,) = wire_calls(TO, message)
        components = payload["template"]["components"]
        assert [c["type"] for c in components] == ["header", "body", "button"]
        assert components[2]["sub_type"] == "url"
        assert components[2]["index"] == "0"

    def test_a_missing_slot_is_padded_so_later_values_keep_their_place(self) -> None:
        """Meta binds parameters positionally, so a gap shifts everything after
        it: supplying only {{2}} and sending one parameter delivers that value
        in {{1}}'s place, to a real contact, with nothing reporting a problem."""
        message = OutboundMessage(template_ref="t/en", template_variables=(("body.2", "SECOND"),))
        (payload,) = wire_calls(TO, message)
        assert payload["template"]["components"][0]["parameters"] == [
            {"type": "text", "text": ""},
            {"type": "text", "text": "SECOND"},
        ]

    def test_a_repeated_slot_collapses_to_one_parameter(self) -> None:
        """Two parameters for one placeholder make the count disagree with the
        template and Meta refuses the whole message."""
        message = OutboundMessage(
            template_ref="t/en",
            template_variables=(("body.1", "first"), ("body.1", "second")),
        )
        (payload,) = wire_calls(TO, message)
        assert payload["template"]["components"][0]["parameters"] == [{"type": "text", "text": "second"}]

    def test_a_slot_numbered_zero_cannot_stretch_the_run(self) -> None:
        message = OutboundMessage(template_ref="t/en", template_variables=(("body.0", "x"), ("body.1", "ok")))
        (payload,) = wire_calls(TO, message)
        assert payload["template"]["components"][0]["parameters"] == [{"type": "text", "text": "ok"}]

    def test_a_slot_that_does_not_parse_is_skipped_rather_than_fatal(self) -> None:
        message = OutboundMessage(template_ref="t/en", template_variables=(("nonsense", "x"), ("body.1", "ok")))
        (payload,) = wire_calls(TO, message)
        assert payload["template"]["components"] == [{"type": "body", "parameters": [{"type": "text", "text": "ok"}]}]

    def test_a_template_with_no_variables_sends_no_components(self) -> None:
        (payload,) = wire_calls(TO, OutboundMessage(template_ref="t/en"))
        assert "components" not in payload["template"]

    def test_the_payload_needs_no_database_row(self) -> None:
        """A retry rebuilds the OutboundMessage from the stored body hours later.

        A payload that needed the template row to still exist would be a retry
        that stops working the moment somebody deletes a template.
        """
        from apps.messaging.rendering import outbound_from_body

        original = OutboundMessage(template_ref="t/en_US", template_variables=(("body.1", "Ada"),))
        assert wire_calls(TO, outbound_from_body(original.to_body())) == wire_calls(TO, original)


@pytest.mark.django_db
class TestSending:
    def test_the_token_travels_in_a_header_and_never_in_the_url(self, tenancy: Any) -> None:
        """Graph accepts ``?access_token=``; httpx logs every URL it requests."""
        with fake_graph_api() as fake:
            WhatsAppAdapter().send(make_connection(tenancy.workspace), _Identity(), text("hi"))
        assert fake.authorizations == [f"Bearer {ACCESS_TOKEN}"]
        assert all(ACCESS_TOKEN not in path for path in fake.paths())

    def test_it_posts_to_the_connections_phone_number(self, tenancy: Any) -> None:
        with fake_graph_api() as fake:
            WhatsAppAdapter().send(make_connection(tenancy.workspace), _Identity(), text("hi"))
        assert fake.paths() == [f"/v21.0/{PHONE_NUMBER_ID}/messages"]

    def test_the_result_carries_metas_message_id(self, tenancy: Any) -> None:
        with fake_graph_api():
            result = WhatsAppAdapter().send(make_connection(tenancy.workspace), _Identity(), text("hi"))
        assert result.status == SendStatus.SENT
        assert result.provider_message_id == "wamid.SENT1"

    def test_a_multi_part_send_reports_the_last_id(self, tenancy: Any) -> None:
        """It is the message the contact is looking at, and the one a receipt
        will reference."""
        ids = iter(["wamid.A", "wamid.B"])

        with fake_graph_api() as fake:
            fake.reply("messages", Reply(body={"messages": [{"id": next(ids)}]}))
            result = WhatsAppAdapter().send(make_connection(tenancy.workspace), _Identity(), text("x" * 9000))
        assert result.provider_message_id == "wamid.A"

    def test_no_recipient_is_a_named_failure_not_an_exception(self, tenancy: Any) -> None:
        result = WhatsAppAdapter().send(make_connection(tenancy.workspace), _Identity(""), text("hi"))
        assert result.status == SendStatus.FAILED
        assert result.error == "no_recipient"

    def test_a_message_with_nothing_sendable_is_reported_not_silently_counted(self, tenancy: Any) -> None:
        result = WhatsAppAdapter().send(make_connection(tenancy.workspace), _Identity(), OutboundMessage())
        assert result.status == SendStatus.FAILED
        assert result.error == "empty_message"

    def test_a_429_becomes_a_rate_limit_error_the_pipeline_reschedules(self, tenancy: Any) -> None:
        """Inherited from providers.base, not re-implemented here."""
        with fake_graph_api() as fake:
            fake.reply("messages", Reply(status=httpx.codes.TOO_MANY_REQUESTS, headers={"Retry-After": "30"}))
            with pytest.raises(RateLimitError) as caught:
                WhatsAppAdapter().send(make_connection(tenancy.workspace), _Identity(), text("hi"))
        assert caught.value.retry_after == 30

    def test_a_rejection_carries_metas_code_and_not_its_prose(self, tenancy: Any) -> None:
        with fake_graph_api() as fake:
            fake.reply("messages", Reply(status=400))
            with pytest.raises(APIError) as caught:
                WhatsAppAdapter().send(make_connection(tenancy.workspace), _Identity(), text("hi"))
        assert caught.value.code == "400"
        assert "graph.facebook.com" in str(caught.value)
        assert PHONE_NUMBER_ID not in str(caught.value)
