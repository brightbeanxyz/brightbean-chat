"""Reading an OutboundMessage back out of a stored body.

A retry happens hours later in a different process with the caller long gone, so
the row has to be enough. That makes this the inverse of
``OutboundMessage.to_body()``, and a round trip is the assertion that matters.
"""

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
from apps.messaging.rendering import outbound_from_body

SHAPES = {
    "empty": OutboundMessage(),
    "text": OutboundMessage(blocks=(TextBlock(text="hello"),)),
    "media": OutboundMessage(blocks=(MediaBlock(kind="image", url="https://x.test/a.png", caption="cap"),)),
    "buttons": OutboundMessage(
        blocks=(TextBlock(text="pick"),),
        buttons=(Button(id="b1", label="Yes"), Button(id="b2", label="Docs", url="https://x.test")),
    ),
    "quick_replies": OutboundMessage(quick_replies=(QuickReply(id="q1", label="Sure"),)),
    "card": OutboundMessage(
        blocks=(
            CardBlock(
                card=Card(
                    title="T",
                    subtitle="S",
                    image_url="https://x.test/i.png",
                    url="https://x.test",
                    buttons=(Button(id="b", label="Go"),),
                )
            ),
        )
    ),
    "gallery": OutboundMessage(blocks=(GalleryBlock(cards=(Card(title="one"), Card(title="two"))),)),
    "tagged": OutboundMessage(blocks=(TextBlock(text="hi"),), tag="ACCOUNT_UPDATE"),
    "templated": OutboundMessage(template_ref="tpl-1"),
    "everything": OutboundMessage(
        blocks=(TextBlock(text="hi"), MediaBlock(kind="video", url="https://x.test/v.mp4")),
        buttons=(Button(id="b1", label="Yes"),),
        quick_replies=(QuickReply(id="q1", label="Sure"),),
        tag="ACCOUNT_UPDATE",
        template_ref="tpl-2",
    ),
}


@pytest.mark.parametrize("name", sorted(SHAPES))
def test_a_body_round_trips(name: str) -> None:
    original = SHAPES[name]
    assert outbound_from_body(original.to_body()).to_body() == original.to_body()


class TestDefensiveness:
    """The body it reads may be from an older release, hand-edited in the admin,
    or a shape a later block type introduced. None of that may raise on a retry."""

    @pytest.mark.parametrize("body", [None, "", 42, [], {"blocks": "not-a-list"}, {"blocks": [1, 2]}])
    def test_a_malformed_body_yields_an_empty_message(self, body: Any) -> None:
        assert outbound_from_body(body).to_body() == OutboundMessage().to_body()

    def test_an_unknown_block_type_is_dropped_rather_than_guessed_at(self) -> None:
        """Sending less than intended is visible in the thread; sending
        something wrong is not."""
        body = {"blocks": [{"type": "hologram", "payload": "?"}, {"type": "text", "text": "kept"}]}
        result = outbound_from_body(body)
        assert len(result.blocks) == 1

    def test_a_button_with_no_id_is_dropped(self) -> None:
        """It could never be matched when it came back as a postback, so it
        would be a control the flow engine can never resume from."""
        assert outbound_from_body({"buttons": [{"label": "orphan"}]}).buttons == ()

    def test_a_media_block_with_no_url_is_dropped(self) -> None:
        assert outbound_from_body({"blocks": [{"type": "image", "url": ""}]}).blocks == ()

    def test_hostile_strings_survive_as_data(self) -> None:
        from apps.messaging.tests.hostile import INJECTIONS

        for payload in INJECTIONS:
            body = {"blocks": [{"type": "text", "text": payload}]}
            block = outbound_from_body(body).blocks[0]
            assert isinstance(block, TextBlock)
            assert block.text == payload
