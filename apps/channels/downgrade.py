"""Deterministic block downgrading (SPEC §6.1).

    The engine renders one abstract ``OutboundMessage``; the adapter downgrades
    unsupported blocks deterministically: gallery -> sequential image+text
    messages; buttons unsupported -> numbered options appended to text ("Reply 1
    for ..."); card -> image + text + url in text.

This module is that renderer, written once rather than once per adapter — six
implementations of "approximately this" is six subtly different products.

**Pure by construction.** No Django import, no database, no clock, no
randomness, no I/O. Same input, same output, forever; which is what lets
``tests/test_downgrade.py`` be a table, and what lets L2-D's builder preview a
downgrade at edit time with no connection in existence.

**Idempotent.** Downgrading an already-downgraded message returns it unchanged.
The renderer sits on the send path, and a retry that re-rendered would otherwise
renumber the options a contact is still looking at.

Numbering runs **continuously across the whole result**, not per message. A
three-card gallery whose cards each carry a button becomes three messages, and
if each restarted at 1 a contact replying "1" would be answering an ambiguous
question. The mapping returned here is the answer key L4-A matches a numeric
reply against.

The order of operations is fixed, and is the order below — galleries expand
before cards downgrade, cards before media, media before buttons, buttons before
the length cap — because every step can create work for the next.
"""

from dataclasses import dataclass, field

from apps.channels.capabilities import Capabilities
from apps.channels.events import (
    Block,
    Button,
    CardBlock,
    GalleryBlock,
    MediaBlock,
    OutboundMessage,
    QuickReply,
    TextBlock,
)

__all__ = ["DowngradeResult", "downgrade"]

#: How a numbered option reads. SPEC §6.1 fixes the wording.
NUMBERED_OPTION = "Reply {number} for {label}"

#: How a URL button reads once inlined into the text.
INLINE_URL = "{label}: {url}"


@dataclass(frozen=True)
class DowngradeResult:
    """What the platform will actually receive.

    ``messages``
        Sent in order, one per platform send. A gallery becomes several.
    ``numeric_replies``
        ``{"1": button_or_quick_reply_id}``, empty when nothing was numbered.
        L4-A matches an inbound numeric reply against it to recover the button
        press the platform could not represent.
    ``notes``
        One line per downgrade applied. L2-D's builder surfaces these as
        capability warnings; the tests assert the *reason* was what we thought.
    """

    messages: tuple[OutboundMessage, ...]
    numeric_replies: dict[str, str]
    notes: tuple[str, ...]


@dataclass
class _Pending:
    """One outgoing message under construction.

    Interaction is tracked per message rather than only on the last one: a
    downgraded gallery card carries its own buttons, and on a platform with
    reply buttons but no carousel (WhatsApp) those survive as real buttons on
    that card's message instead of collapsing into text.
    """

    blocks: list[Block] = field(default_factory=list)
    buttons: list[Button] = field(default_factory=list)
    quick_replies: list[QuickReply] = field(default_factory=list)
    #: Lines destined for the end of this message's text: numbered options and
    #: inlined URLs.
    trailer: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.blocks or self.buttons or self.quick_replies or self.trailer)


def downgrade(outbound: OutboundMessage, capabilities: Capabilities) -> DowngradeResult:
    """Render ``outbound`` into what ``capabilities`` can carry."""
    state = _State(capabilities)

    # 1. Blocks. Galleries expand into whole messages; cards and unsupported
    #    media collapse into the message being built.
    for block in outbound.blocks:
        state.add_block(block)

    # 2. Interaction on the message itself, which rides on the final message.
    state.resolve_buttons(outbound.buttons, state.current)
    state.resolve_quick_replies(outbound.quick_replies, state.current)

    # 3. Text: numbering appended, then the length cap — in that order, because
    #    the numbering is text too.
    return state.finish(tag=outbound.tag, template_ref=outbound.template_ref)


class _State:
    """The renderer's working memory. One instance per :func:`downgrade` call."""

    def __init__(self, capabilities: Capabilities) -> None:
        self.caps = capabilities
        # Always at least one, so `current` is never a special case.
        self.pending: list[_Pending] = [_Pending()]
        self.numeric_replies: dict[str, str] = {}
        self.notes: list[str] = []

    @property
    def current(self) -> _Pending:
        return self.pending[-1]

    def start_message(self) -> None:
        """Begin a new outgoing message unless the current one is untouched."""
        if not self.current.is_empty():
            self.pending.append(_Pending())

    # -- blocks -------------------------------------------------------------

    def add_block(self, block: Block) -> None:
        if isinstance(block, GalleryBlock):
            self.add_gallery(block)
        elif isinstance(block, CardBlock):
            self.add_card(block)
        elif isinstance(block, MediaBlock):
            self.add_media(block)
        else:
            self.current.blocks.append(block)

    def add_gallery(self, block: GalleryBlock) -> None:
        """gallery -> sequential messages, one per card (SPEC §6.1)."""
        if self.caps.gallery:
            self.current.blocks.append(block)
            return
        self.notes.append(f"gallery: {len(block.cards)} cards sent as sequential messages")
        for card in block.cards:
            self.start_message()
            self.add_card(CardBlock(card=card))

    def add_card(self, block: CardBlock) -> None:
        """card -> image + text + url in text (SPEC §6.1)."""
        if self.caps.card:
            self.current.blocks.append(block)
            return
        self.notes.append("card: rendered as image + text")
        card = block.card
        if card.image_url:
            # add_media handles a platform that cannot take the image either.
            self.add_media(MediaBlock(kind="image", url=card.image_url))
        lines = [line for line in (card.title, card.subtitle) if line]
        if card.url:
            lines.append(card.url)
        if lines:
            self.current.blocks.append(TextBlock(text="\n".join(lines)))
        # The card's own buttons have no card left to hang off, so they compete
        # for this message's button slots and overflow into its text.
        self.resolve_buttons(card.buttons, self.current)

    def add_media(self, block: MediaBlock) -> None:
        """An unsupported media kind degrades to its caption plus the URL."""
        if self.caps.supports_block(block.kind):
            self.current.blocks.append(block)
            return
        self.notes.append(f"{block.kind}: not supported, sent as a link")
        text = "\n".join(part for part in (block.caption, block.url) if part)
        if text:
            self.current.blocks.append(TextBlock(text=text))

    # -- interaction --------------------------------------------------------

    def resolve_buttons(self, buttons: tuple[Button, ...], target: _Pending) -> None:
        """Fill ``target``'s button slots; number or inline whatever is left.

        URL and postback buttons fail differently. A URL button without URL
        button support is still useful as a link in the text; a postback button
        is only recoverable as a numbered option the contact types back.
        """
        if not buttons:
            return

        supports = self.caps.buttons and self.caps.max_buttons > 0
        room = self.caps.max_buttons - len(target.buttons) if supports else 0
        overflow: list[Button] = []

        for button in buttons:
            usable = supports and (not button.is_url or self.caps.url_buttons)
            if usable and room > 0:
                target.buttons.append(button)
                room -= 1
            else:
                overflow.append(button)

        if not overflow:
            return
        reason = "not supported" if not supports else f"over the {self.caps.max_buttons}-button limit"
        self.notes.append(f"buttons: {len(overflow)} {reason}, appended to the text")
        for button in overflow:
            if button.is_url:
                target.trailer.append(INLINE_URL.format(label=button.label, url=button.url))
            else:
                target.trailer.append(self.number(button.id, button.label))

    def resolve_quick_replies(self, quick_replies: tuple[QuickReply, ...], target: _Pending) -> None:
        """Same rule for quick replies: keep what fits, number the rest."""
        if not quick_replies:
            return

        supports = self.caps.quick_replies and self.caps.max_quick_replies > 0
        # Symmetric with resolve_buttons: count what this message already holds.
        # Only one call per message reaches this today, but resolve_buttons is
        # already called twice against one target (once per downgraded card,
        # once for the message), and the asymmetry would overfill the moment
        # anything else contributed quick replies.
        room = max(0, self.caps.max_quick_replies - len(target.quick_replies)) if supports else 0
        target.quick_replies.extend(quick_replies[:room])
        overflow = quick_replies[room:]

        if not overflow:
            return
        reason = "not supported" if not supports else f"over the {room}-reply limit"
        self.notes.append(f"quick replies: {len(overflow)} {reason}, appended to the text")
        for quick_reply in overflow:
            target.trailer.append(self.number(quick_reply.id, quick_reply.label))

    def number(self, target_id: str, label: str) -> str:
        """Allocate the next reply number to ``target_id`` and return its line."""
        number = str(len(self.numeric_replies) + 1)
        self.numeric_replies[number] = target_id
        return NUMBERED_OPTION.format(number=number, label=label)

    # -- assembly -----------------------------------------------------------

    def finish(self, *, tag: str | None, template_ref: str | None) -> DowngradeResult:
        """Append trailers, apply the length cap, and freeze into messages."""
        messages: list[OutboundMessage] = []
        for pending in self.pending:
            self.append_trailer(pending)
            blocks = self.apply_text_cap(pending.blocks)
            messages.append(
                OutboundMessage(
                    blocks=tuple(blocks),
                    buttons=tuple(pending.buttons),
                    quick_replies=tuple(pending.quick_replies),
                    tag=tag,
                    template_ref=template_ref,
                )
            )

        # A message that ended up with nothing to say is dropped — a gallery
        # card with no image, title or URL contributes none. The last one stays
        # regardless: an empty outbound must still be one message rather than
        # zero, or a caller counting sends sees nothing happen.
        kept = tuple(m for index, m in enumerate(messages) if m.blocks or m.buttons or m.quick_replies) or (
            messages[-1],
        )
        return DowngradeResult(
            messages=kept,
            numeric_replies=dict(self.numeric_replies),
            notes=tuple(self.notes),
        )

    @staticmethod
    def append_trailer(pending: _Pending) -> None:
        """Put the numbered options at the end of the message's text."""
        if not pending.trailer:
            return
        trailer = "\n".join(pending.trailer)
        for index in range(len(pending.blocks) - 1, -1, -1):
            block = pending.blocks[index]
            if isinstance(block, TextBlock):
                pending.blocks[index] = TextBlock(text=f"{block.text}\n\n{trailer}")
                pending.trailer = []
                return
        # Nothing to append to — a media-only message, say. The options still
        # have to reach the contact, so they get a block of their own.
        pending.blocks.append(TextBlock(text=trailer))
        pending.trailer = []

    def apply_text_cap(self, blocks: list[Block]) -> list[Block]:
        """Split over-long text on word boundaries (SPEC §6.1's max_text_len)."""
        limit = self.caps.max_text_len
        out: list[Block] = []
        for block in blocks:
            if not isinstance(block, TextBlock) or len(block.text) <= limit:
                out.append(block)
                continue
            parts = _split_text(block.text, limit)
            if parts:
                self.notes.append(f"text: {len(block.text)} characters split into {len(parts)} parts")
            out.extend(TextBlock(text=part) for part in parts)
        return out


def _split_text(text: str, limit: int) -> list[str]:
    """Break ``text`` into ``limit``-sized pieces, preferring word boundaries.

    A single word longer than the limit is cut mid-word: the alternative is
    emitting a piece the platform rejects outright, and a URL long enough to hit
    a 1000-character limit is not something to be precious about.

    Two things this has to survive, because it is a pure function on the send
    path with no way to report a problem:

    ``limit`` below 1
        Would make ``cut`` zero on every pass, so ``remaining`` never shrank and
        the loop never ended — a hung request thread. A platform that accepts no
        text at all is not a thing, but ``Capabilities`` is public and
        ``capabilities_cache`` is documented as narrowing limits per connection,
        so the value is constructible. Clamped rather than raised: one character
        per message is visibly wrong, which is what you want, and a renderer that
        throws mid-send is worse.

    empty pieces
        A run of whitespace filling the whole window used to yield ``""`` as a
        part, which became an empty ``TextBlock`` and then a blank message that
        Telegram and Meta both reject. Empty pieces are skipped.
    """
    limit = max(1, limit)
    parts: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = max(window.rfind(" "), window.rfind("\n"))
        if cut <= 0:
            cut = limit
        piece = remaining[:cut].rstrip()
        if piece:
            parts.append(piece)
        remaining = remaining[cut:].lstrip()
    if remaining:
        parts.append(remaining)
    return parts
