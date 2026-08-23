"""The downgrade renderer, as a table (SPEC §6.1).

A table because the renderer is a pure function of (message, capabilities) and
the interesting cases are combinations: a gallery on a platform with cards but
no carousel behaves differently from one on a platform with neither, and both
differ again when the buttons overflow. Written as prose tests, the combinations
that matter are the ones nobody writes.
"""

import pytest

from apps.channels.capabilities import Capabilities, capabilities_for
from apps.channels.downgrade import downgrade
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
from apps.common.platforms import Platform

# --- capability fixtures ----------------------------------------------------
# Named for what they can do rather than for a platform, so a test says why it
# expects an outcome.

EVERYTHING = Capabilities(
    image=True,
    audio=True,
    video=True,
    file=True,
    card=True,
    gallery=True,
    buttons=True,
    quick_replies=True,
    url_buttons=True,
    max_buttons=3,
    max_quick_replies=13,
    max_text_len=2000,
)
TEXT_ONLY = Capabilities(max_text_len=1600)
CARDS_NO_CAROUSEL = Capabilities(image=True, card=True, buttons=True, url_buttons=True, max_buttons=3)
BUTTONS_NO_CARDS = Capabilities(image=True, buttons=True, url_buttons=True, max_buttons=3, max_text_len=4096)
NO_URL_BUTTONS = Capabilities(image=True, buttons=True, url_buttons=False, max_buttons=3, max_text_len=4096)


def texts(message: OutboundMessage) -> list[str]:
    return [block.text for block in message.blocks if isinstance(block, TextBlock)]


def all_text(result: object) -> str:
    return "\n".join(text for message in result.messages for text in texts(message))  # type: ignore[attr-defined]


CARD = Card(title="Plan A", subtitle="Best value", image_url="https://x.test/a.png", url="https://x.test/a")


class TestNothingToDo:
    def test_a_fully_supported_message_passes_through_unchanged(self) -> None:
        message = OutboundMessage(
            blocks=(TextBlock(text="hi"), MediaBlock(kind="image", url="https://x.test/i.png")),
            buttons=(Button(id="b1", label="Pricing"),),
            quick_replies=(QuickReply(id="q1", label="Yes"),),
        )
        result = downgrade(message, EVERYTHING)
        assert result.messages == (message,)
        assert result.numeric_replies == {}
        assert result.notes == ()

    def test_an_empty_message_is_still_one_message(self) -> None:
        # A caller counting sends must not see zero where it asked for one.
        result = downgrade(OutboundMessage(), TEXT_ONLY)
        assert len(result.messages) == 1


class TestGallery:
    def test_gallery_becomes_sequential_messages(self) -> None:
        cards = (CARD, Card(title="Plan B"), Card(title="Plan C"))
        result = downgrade(OutboundMessage(blocks=(GalleryBlock(cards=cards),)), BUTTONS_NO_CARDS)
        assert len(result.messages) == 3
        assert "Plan A" in all_text(result)
        assert "Plan C" in all_text(result)
        assert any("gallery" in note for note in result.notes)

    def test_gallery_survives_where_the_platform_has_carousels(self) -> None:
        gallery = GalleryBlock(cards=(CARD,))
        result = downgrade(OutboundMessage(blocks=(gallery,)), EVERYTHING)
        assert result.messages[0].blocks == (gallery,)

    def test_a_native_card_keeps_its_own_buttons(self) -> None:
        """Cards survive on a platform with cards but no carousel; so do their buttons."""
        cards = (
            Card(title="A", buttons=(Button(id="a", label="Pick A"),)),
            Card(title="B", buttons=(Button(id="b", label="Pick B"),)),
        )
        result = downgrade(OutboundMessage(blocks=(GalleryBlock(cards=cards),)), CARDS_NO_CAROUSEL)
        assert len(result.messages) == 2
        rendered = [block.card for m in result.messages for block in m.blocks if isinstance(block, CardBlock)]
        assert [card.buttons[0].id for card in rendered] == ["a", "b"]

    def test_a_downgraded_cards_buttons_become_that_messages_buttons(self) -> None:
        """No card to hang them off, but the platform still has buttons — so use them."""
        cards = (
            Card(title="A", buttons=(Button(id="a", label="Pick A"),)),
            Card(title="B", buttons=(Button(id="b", label="Pick B"),)),
        )
        result = downgrade(OutboundMessage(blocks=(GalleryBlock(cards=cards),)), BUTTONS_NO_CARDS)
        assert len(result.messages) == 2
        assert [b.id for b in result.messages[0].buttons] == ["a"]
        assert [b.id for b in result.messages[1].buttons] == ["b"]

    def test_numbering_is_continuous_across_a_gallery(self) -> None:
        """Otherwise a contact replying "1" is answering an ambiguous question."""
        cards = (
            Card(title="A", buttons=(Button(id="a", label="Pick A"),)),
            Card(title="B", buttons=(Button(id="b", label="Pick B"),)),
        )
        result = downgrade(OutboundMessage(blocks=(GalleryBlock(cards=cards),)), TEXT_ONLY)
        assert result.numeric_replies == {"1": "a", "2": "b"}
        assert "Reply 1 for Pick A" in all_text(result)
        assert "Reply 2 for Pick B" in all_text(result)


class TestCard:
    def test_card_becomes_image_plus_text_plus_url(self) -> None:
        result = downgrade(OutboundMessage(blocks=(CardBlock(card=CARD),)), BUTTONS_NO_CARDS)
        message = result.messages[0]
        assert any(isinstance(b, MediaBlock) and b.url == CARD.image_url for b in message.blocks)
        body = "\n".join(texts(message))
        assert "Plan A" in body
        assert "Best value" in body
        assert "https://x.test/a" in body

    def test_card_image_degrades_to_a_link_on_a_text_only_platform(self) -> None:
        result = downgrade(OutboundMessage(blocks=(CardBlock(card=CARD),)), TEXT_ONLY)
        assert all(isinstance(b, TextBlock) for b in result.messages[0].blocks)
        assert CARD.image_url in all_text(result)

    def test_card_survives_where_the_platform_has_cards(self) -> None:
        block = CardBlock(card=CARD)
        assert downgrade(OutboundMessage(blocks=(block,)), EVERYTHING).messages[0].blocks == (block,)


class TestMedia:
    @pytest.mark.parametrize("kind", ["image", "audio", "video", "file"])
    def test_unsupported_media_becomes_a_link(self, kind: str) -> None:
        block = MediaBlock(kind=kind, url=f"https://x.test/f.{kind}", caption="Look")
        result = downgrade(OutboundMessage(blocks=(block,)), TEXT_ONLY)
        assert "Look" in all_text(result)
        assert block.url in all_text(result)
        assert not [b for b in result.messages[0].blocks if isinstance(b, MediaBlock)]

    def test_supported_media_is_left_alone(self) -> None:
        block = MediaBlock(kind="image", url="https://x.test/i.png")
        assert downgrade(OutboundMessage(blocks=(block,)), EVERYTHING).messages[0].blocks == (block,)


class TestButtons:
    def test_unsupported_buttons_become_numbered_options(self) -> None:
        message = OutboundMessage(
            blocks=(TextBlock(text="Pick one"),),
            buttons=(Button(id="p", label="Pricing"), Button(id="d", label="Demo")),
        )
        result = downgrade(message, TEXT_ONLY)
        assert result.numeric_replies == {"1": "p", "2": "d"}
        assert "Reply 1 for Pricing" in all_text(result)
        assert "Reply 2 for Demo" in all_text(result)
        assert result.messages[0].buttons == ()

    def test_overflow_beyond_the_cap_is_numbered(self) -> None:
        buttons = tuple(Button(id=f"b{i}", label=f"Option {i}") for i in range(5))
        result = downgrade(OutboundMessage(blocks=(TextBlock(text="hi"),), buttons=buttons), BUTTONS_NO_CARDS)
        assert [b.id for b in result.messages[0].buttons] == ["b0", "b1", "b2"]
        assert result.numeric_replies == {"1": "b3", "2": "b4"}

    def test_url_buttons_are_inlined_rather_than_numbered(self) -> None:
        """A link is still useful as a link; a postback is only useful as a number."""
        message = OutboundMessage(
            blocks=(TextBlock(text="hi"),),
            buttons=(Button(id="u", label="Docs", url="https://x.test/docs"),),
        )
        result = downgrade(message, NO_URL_BUTTONS)
        assert result.numeric_replies == {}
        assert "Docs: https://x.test/docs" in all_text(result)

    def test_numbered_options_get_their_own_paragraph(self) -> None:
        message = OutboundMessage(blocks=(TextBlock(text="Pick one"),), buttons=(Button(id="p", label="Pricing"),))
        assert texts(downgrade(message, TEXT_ONLY).messages[0]) == ["Pick one\n\nReply 1 for Pricing"]

    def test_numbering_gets_a_text_block_when_there_is_none(self) -> None:
        # Images supported, buttons not: the image stays as a block, and the
        # numbered option has nothing to append to. It must not be dropped.
        caps = Capabilities(image=True, max_text_len=4096)
        message = OutboundMessage(
            blocks=(MediaBlock(kind="image", url="https://x.test/i.png"),),
            buttons=(Button(id="p", label="Pricing"),),
        )
        result = downgrade(message, caps)
        assert isinstance(result.messages[0].blocks[0], MediaBlock)
        assert "Reply 1 for Pricing" in all_text(result)
        assert result.numeric_replies == {"1": "p"}


class TestQuickReplies:
    def test_unsupported_quick_replies_become_numbered_options(self) -> None:
        message = OutboundMessage(
            blocks=(TextBlock(text="?"),),
            quick_replies=(QuickReply(id="y", label="Yes"), QuickReply(id="n", label="No")),
        )
        result = downgrade(message, TEXT_ONLY)
        assert result.numeric_replies == {"1": "y", "2": "n"}
        assert result.messages[0].quick_replies == ()

    def test_overflow_beyond_the_cap_is_numbered(self) -> None:
        caps = Capabilities(quick_replies=True, max_quick_replies=2, max_text_len=4096)
        replies = tuple(QuickReply(id=f"q{i}", label=f"Q{i}") for i in range(4))
        result = downgrade(OutboundMessage(blocks=(TextBlock(text="?"),), quick_replies=replies), caps)
        assert [q.id for q in result.messages[0].quick_replies] == ["q0", "q1"]
        assert result.numeric_replies == {"1": "q2", "2": "q3"}

    def test_buttons_are_numbered_before_quick_replies(self) -> None:
        """Fixed order, so the same message always numbers the same way."""
        message = OutboundMessage(
            blocks=(TextBlock(text="?"),),
            buttons=(Button(id="b", label="Button"),),
            quick_replies=(QuickReply(id="q", label="Quick"),),
        )
        assert downgrade(message, TEXT_ONLY).numeric_replies == {"1": "b", "2": "q"}


class TestTextLength:
    def test_over_long_text_splits_on_word_boundaries(self) -> None:
        caps = Capabilities(max_text_len=20)
        result = downgrade(OutboundMessage(blocks=(TextBlock(text="alpha bravo charlie delta echo"),)), caps)
        parts = texts(result.messages[0])
        assert len(parts) > 1
        assert all(len(part) <= 20 for part in parts)
        # Nothing lost, nothing cut mid-word.
        assert " ".join(parts) == "alpha bravo charlie delta echo"

    def test_a_single_over_long_word_is_cut(self) -> None:
        caps = Capabilities(max_text_len=10)
        result = downgrade(OutboundMessage(blocks=(TextBlock(text="x" * 25),)), caps)
        parts = texts(result.messages[0])
        assert [len(part) for part in parts] == [10, 10, 5]

    def test_the_cap_applies_after_numbering(self) -> None:
        """Numbering is text too, so it cannot be allowed to push past the cap."""
        caps = Capabilities(max_text_len=30)
        message = OutboundMessage(
            blocks=(TextBlock(text="Choose"),),
            buttons=(Button(id="a", label="A rather long option label"),),
        )
        result = downgrade(message, caps)
        assert all(len(text) <= 30 for text in texts(result.messages[0]))


class TestSplittingEdgeCases:
    """Regressions in split_text, which is pure and has no way to complain."""

    def test_leading_whitespace_does_not_produce_an_empty_message(self) -> None:
        """An empty TextBlock is a blank message, which platforms reject."""
        caps = Capabilities(max_text_len=10)
        result = downgrade(OutboundMessage(blocks=(TextBlock(text=" " * 10 + "abcdefghijk"),)), caps)
        parts = texts(result.messages[0])
        assert parts == ["abcdefghij", "k"]
        assert all(part.strip() for part in parts)

    @pytest.mark.parametrize("limit", [0, -1])
    def test_a_non_positive_limit_terminates(self, limit: int) -> None:
        """limit <= 0 used to leave `remaining` unchanged and spin forever."""
        caps = Capabilities(max_text_len=limit)
        result = downgrade(OutboundMessage(blocks=(TextBlock(text="abc"),)), caps)
        assert texts(result.messages[0]) == ["a", "b", "c"]

    def test_an_all_whitespace_block_drops_out_rather_than_splitting_into_blanks(self) -> None:
        caps = Capabilities(max_text_len=4)
        result = downgrade(OutboundMessage(blocks=(TextBlock(text=" " * 20),)), caps)
        assert texts(result.messages[0]) == []


class TestQuickReplyRoom:
    def test_a_second_resolve_does_not_exceed_the_cap(self) -> None:
        """resolve_quick_replies must count what the message already holds."""
        from apps.channels.downgrade import _Pending, _State

        caps = Capabilities(quick_replies=True, max_quick_replies=2, max_text_len=999)
        state = _State(caps)
        target = _Pending()
        first = (QuickReply(id="a", label="A"), QuickReply(id="b", label="B"))
        second = (QuickReply(id="c", label="C"),)

        state.resolve_quick_replies(first, target)
        state.resolve_quick_replies(second, target)

        assert len(target.quick_replies) == 2
        assert state.numeric_replies == {"1": "c"}


class TestIdempotence:
    """Downgrading twice equals downgrading once — the send path may retry."""

    @pytest.mark.parametrize("platform", Platform.values)
    def test_every_platform(self, platform: str) -> None:
        caps = capabilities_for(platform)
        message = OutboundMessage(
            blocks=(
                TextBlock(text="Hello"),
                MediaBlock(kind="video", url="https://x.test/v.mp4", caption="Watch"),
                GalleryBlock(cards=(CARD, Card(title="Plan B", buttons=(Button(id="b", label="Pick B"),)))),
            ),
            buttons=(Button(id="p", label="Pricing"), Button(id="d", label="Demo", url="https://x.test/demo")),
            quick_replies=(QuickReply(id="y", label="Yes"),),
        )
        once = downgrade(message, caps)
        for rendered in once.messages:
            again = downgrade(rendered, caps)
            assert again.messages == (rendered,), platform
            assert again.numeric_replies == {}, platform


class TestPurity:
    def test_the_input_is_never_mutated(self) -> None:
        message = OutboundMessage(
            blocks=(TextBlock(text="hi"), GalleryBlock(cards=(CARD,))),
            buttons=(Button(id="p", label="Pricing"),),
        )
        before = message.to_body()
        downgrade(message, TEXT_ONLY)
        assert message.to_body() == before

    def test_the_same_input_renders_identically(self) -> None:
        message = OutboundMessage(
            blocks=(GalleryBlock(cards=(CARD, Card(title="B", buttons=(Button(id="b", label="B"),)))),),
            buttons=(Button(id="p", label="Pricing"),),
        )
        first = downgrade(message, TEXT_ONLY)
        second = downgrade(message, TEXT_ONLY)
        assert first == second
