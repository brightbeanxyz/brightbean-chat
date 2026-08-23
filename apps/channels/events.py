"""The normalized event and message schemas (SPEC §7.2).

Two directions, one vocabulary:

``NormalizedEvent``
    What an adapter turns a platform's webhook payload *into*. Everything
    downstream — L3-A's persistence, L4-A's trigger matcher — reads this and
    never the raw provider JSON, so a new platform costs a parser rather than a
    branch in the routing code.

``OutboundMessage``
    What the flow engine renders *once*, abstractly, for every platform. The
    adapter (via :mod:`apps.channels.downgrade`) is what turns it into whatever
    the platform can actually carry.

Everything here is a frozen dataclass with tuple collections. Two reasons, both
learned the hard way elsewhere in this repo: these objects are passed to a chain
of processors (contract 6) that must not be able to mutate each other's input,
and a mutable default on a shared dataclass is a bug that only shows up under
concurrency.

Deliberately Django-free at runtime — ``connection`` is typed through
``TYPE_CHECKING`` — so the downgrade renderer and its table tests never need the
app registry.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from apps.channels.models import ChannelConnection

__all__ = [
    "Button",
    "Card",
    "CardBlock",
    "EventPayload",
    "EventType",
    "GalleryBlock",
    "MediaBlock",
    "NormalizedEvent",
    "OutboundMessage",
    "QuickReply",
    "SendResult",
    "SendStatus",
    "TextBlock",
]


# ---------------------------------------------------------------------------
# Inbound
# ---------------------------------------------------------------------------


class EventType(StrEnum):
    """SPEC §7.2's event types, plus one the spec's own §6.3 forces.

    A ``StrEnum`` so ``event.type == "message"`` reads true and the value
    serialises into the event log without a conversion step.

    ``MESSAGE_DELETED`` is the addition, and it is deliberate rather than
    incidental. SPEC §7.2 lists nine types; SPEC §6.3 separately requires
    Instagram's ``message_deletions`` webhook field to be handled ("redact
    message body, keep row with status deleted") and §19 repeats it, so the
    signal has to arrive as *something*. The alternative considered was a
    ``DELIVERY_STATUS`` event carrying ``status="deleted"``, which would mean
    widening ``apps.messaging.ingest.RECEIPT_STATUSES`` and the pure
    ``_next_status`` ladder — both deliberately narrow, and neither is about
    deletion. A deletion is not a rung on the delivery ladder; it is the row
    being retracted. So it gets its own type, and this enum is shared, so the
    addition is called out in the PR that made it (#17, L5-A).
    """

    MESSAGE = "message"
    POSTBACK = "postback"
    COMMENT = "comment"
    STORY_MENTION = "story_mention"
    STORY_REPLY = "story_reply"
    REFERRAL = "referral"
    FOLLOW = "follow"
    DELIVERY_STATUS = "delivery_status"
    #: The platform says a message it delivered no longer exists. SPEC §6.3.
    MESSAGE_DELETED = "message_deleted"
    OPT_OUT = "opt_out"


@dataclass(frozen=True)
class EventPayload:
    """SPEC §7.2's payload: "text, attachments, button id, comment id, media ids, ref string".

    Every field is optional and defaulted. Webhook payloads are
    attacker-controlled (SECURITY-BASELINE §2), so an adapter that finds a
    missing or wrongly typed key leaves the field at its default rather than
    raising — one malformed event must not cost the whole delivery.

    ``extra`` is the escape hatch for platform-specific detail a later adapter
    needs (a Telegram chat id, a Meta story id) without widening this class for
    every platform. It is untrusted data like the rest.
    """

    text: str = ""
    #: Media URLs the platform delivered. Attacker-controlled: never fetched
    #: server-side (SECURITY-BASELINE §6 forbids it until the SSRF guard lands).
    attachments: tuple[str, ...] = ()
    button_id: str = ""
    comment_id: str = ""
    media_ids: tuple[str, ...] = ()
    #: The ref string from a t.me/?start= or m.me/ deep link — SPEC §10's Ref
    #: URL trigger reads it.
    ref: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class NormalizedEvent:
    """One inbound event, platform-shaped detail already stripped.

    **A dispatched event is read-only.** The dataclass is frozen, but ``raw`` and
    ``payload.extra`` are ordinary dicts: a processor on the contract-6 seam
    that mutates one in place changes what every later processor sees.
    :func:`apps.channels.ingest.process_events` hands out an immutable sequence
    to make the structural half of that impossible; the nested half is a
    convention, and this is where it is written down.

    ``provider_event_id`` is the deduplication key (SPEC §7.1 step 2). An
    adapter whose platform does not supply a stable id per event must synthesise
    a deterministic one — :func:`apps.channels.ingest.synthetic_event_id` does
    it from the raw payload — because "no id" would mean every retry of a
    delivery is processed again.
    """

    type: EventType
    connection: "ChannelConnection"
    platform_user_id: str
    provider_event_id: str
    timestamp: datetime
    payload: EventPayload = field(default_factory=EventPayload)
    #: The slice of the provider's delivery this event came from, as delivered.
    #: Stored verbatim in ``webhook_event_log.raw`` (SPEC §5) so an operator
    #: debugging a mis-parse can see what actually arrived. Per event rather
    #: than per delivery, because a Meta batch carries several unrelated events
    #: and the log row is the event. Attacker-controlled: escape on render.
    raw: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Outbound
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Button:
    """A pressable button.

    ``url`` set makes it a URL button; unset makes it a postback whose ``id``
    comes back as ``EventPayload.button_id``. The distinction matters to the
    downgrade renderer: a platform without URL buttons gets the link inlined in
    text, while a platform without postback buttons gets a numbered option.
    """

    id: str
    label: str
    url: str = ""

    @property
    def is_url(self) -> bool:
        return bool(self.url)


@dataclass(frozen=True)
class QuickReply:
    """A one-tap reply chip. Its ``id`` returns as ``EventPayload.button_id``."""

    id: str
    label: str


@dataclass(frozen=True)
class TextBlock:
    text: str
    kind: str = "text"


@dataclass(frozen=True)
class MediaBlock:
    """An image, audio clip, video or file, addressed by URL.

    ``kind`` is one of image/audio/video/file and is matched against
    :meth:`~apps.channels.capabilities.Capabilities.supports_block`.
    """

    kind: str
    url: str
    caption: str = ""


@dataclass(frozen=True)
class Card:
    """One card of a generic template / carousel."""

    title: str = ""
    subtitle: str = ""
    image_url: str = ""
    url: str = ""
    buttons: tuple[Button, ...] = ()


@dataclass(frozen=True)
class CardBlock:
    card: Card
    kind: str = "card"


@dataclass(frozen=True)
class GalleryBlock:
    cards: tuple[Card, ...]
    kind: str = "gallery"


#: Anything that may appear in ``OutboundMessage.blocks``.
type Block = TextBlock | MediaBlock | CardBlock | GalleryBlock


@dataclass(frozen=True)
class OutboundMessage:
    """One abstract message, rendered once for every platform (SPEC §7.2).

    ``tag`` and ``template_ref`` are the compliance hooks: L3-A's ``can_send``
    fills them in when a platform's policy demands a message tag or an approved
    template, and the adapter passes them through.
    """

    blocks: tuple[Block, ...] = ()
    buttons: tuple[Button, ...] = ()
    quick_replies: tuple[QuickReply, ...] = ()
    tag: str | None = None
    template_ref: str | None = None
    #: The values that fill an approved template's ``{{1}}``-style slots, as
    #: ordered ``(slot, value)`` pairs — ``("body.1", "Ada")``, ``("header.1",
    #: "March")``, ``("button.0.1", "order/42")``.
    #:
    #: A tuple of pairs rather than a dict for the reason this whole module is
    #: frozen dataclasses with tuple collections: these objects are handed to a
    #: chain of processors that must not be able to mutate each other's input.
    #: The slot strings are the platform-neutral half — an adapter groups them
    #: into whatever its own template payload looks like — so nothing here
    #: knows what a WhatsApp component is.
    #:
    #: **Already rendered.** Substitution happens in the flow engine, where the
    #: contact and the variable bag are, through the one shared renderer
    #: (SECURITY-BASELINE §3). An adapter receives finished strings and must
    #: never render them again.
    #:
    #: Additive to the SPEC §7.2 shape, like ``node_id`` before it: readers that
    #: do not know the key ignore it, and an older row without it reads back as
    #: empty.
    template_variables: tuple[tuple[str, str], ...] = ()
    #: The flow node this message came from, where one did (issue #12).
    #:
    #: SPEC §6.2 requires Telegram's ``callback_data`` to carry
    #: ``node_id:button_id``, and Meta's postback payloads take the same shape,
    #: so the id has to reach the adapter — and :class:`Button` cannot carry it,
    #: because ``Button.id`` is matched verbatim against the waiting node's
    #: handles (``apps.flows.engine.waits``). Set by
    #: ``apps.flows.engine.sending.deliver``; empty for an agent reply, an API
    #: send, or anything else with no node behind it, and an adapter must treat
    #: empty as "no node" rather than as a node named "".
    #:
    #: Carried in :meth:`to_body` so a retry reproduces the same wire payload.
    #: The retry path rebuilds this object from the stored row hours later
    #: (``apps.messaging.rendering.outbound_from_body``), and a lost node id
    #: there would mean the second attempt's buttons carried different
    #: ``callback_data`` from the first — with both keyboards live in the same
    #: chat. Additive to the SPEC §7.2 shape: readers that do not know the key
    #: ignore it, and an older row without it reads back as "".
    node_id: str = ""
    #: The subject line, for platforms that have one. Email is the only such
    #: platform in v1 (SPEC §6.7, §11.10); every other adapter ignores it.
    #:
    #: A field rather than a block kind, for the reason ``node_id`` is one: the
    #: block vocabulary is walked by ``apps.channels.downgrade`` for every
    #: platform, and a subject is not a thing that can be downgraded into text
    #: — it is an envelope property. Empty means "the adapter picks", which for
    #: email means the connection's configured default.
    subject: str = ""
    #: A per-message From address, overriding the connection's (SPEC §11.10's
    #: ``from_override``). Empty means the connection's own from-address, which
    #: is the case for every send that did not ask for something else. Like
    #: ``subject``, envelope rather than content.
    from_override: str = ""
    #: An authored HTML body, for a platform that renders one. Email is the only
    #: such platform in v1 (SPEC §11.10's ``html_body``).
    #:
    #: **This is the only field in this class whose contents are markup**, and
    #: that is exactly why it is separate. ``TextBlock.text`` is plain text on
    #: every path that produces one — a flow's ``send_message``, an inbox reply,
    #: an API send — so an adapter building HTML has to escape it. Carrying the
    #: author's markup in a block instead would make "is this string HTML?"
    #: depend on which node happened to create it, and the answer would be wrong
    #: for a contact whose name is ``<img src=…>``.
    #:
    #: Set it *and* a ``TextBlock`` holding the plain-text equivalent: the blocks
    #: are what the inbox thread renders, and they should not be raw markup.
    html_body: str = ""

    def to_body(self) -> dict[str, Any]:
        """The SPEC §7.2 ``message.body`` json.

        L3-A stores this on the message row, so the shape is a persisted
        contract: blocks carry their own ``type`` discriminator and every
        top-level key is always present, even when empty. Keys are added
        additively (``node_id`` arrived with issue #12; ``subject`` and
        ``from_override`` with #21) and never removed or renamed — rows written
        by an older release stay readable, which is what
        ``apps.messaging.rendering`` depends on to retry them.
        """
        return {
            "blocks": [_block_json(block) for block in self.blocks],
            "buttons": [_button_json(button) for button in self.buttons],
            "quick_replies": [{"id": qr.id, "label": qr.label} for qr in self.quick_replies],
            "tag": self.tag,
            "template_ref": self.template_ref,
            # As a list of two-element lists: json has no tuples, and a dict
            # keyed by slot would lose the order the components are built in.
            "template_variables": [[slot, value] for slot, value in self.template_variables],
            "node_id": self.node_id,
            "subject": self.subject,
            "from_override": self.from_override,
            "html_body": self.html_body,
        }


def _button_json(button: Button) -> dict[str, Any]:
    return {"id": button.id, "label": button.label, "url": button.url or None}


def _card_json(card: Card) -> dict[str, Any]:
    return {
        "title": card.title,
        "subtitle": card.subtitle,
        "image_url": card.image_url,
        "url": card.url,
        "buttons": [_button_json(button) for button in card.buttons],
    }


def _block_json(block: Block) -> dict[str, Any]:
    """Serialize one block. ``type`` rather than ``kind``, per SPEC §7.2."""
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, MediaBlock):
        return {"type": block.kind, "url": block.url, "caption": block.caption}
    if isinstance(block, CardBlock):
        return {"type": "card", **_card_json(block.card)}
    return {"type": "gallery", "cards": [_card_json(card) for card in block.cards]}


# ---------------------------------------------------------------------------
# Send results
# ---------------------------------------------------------------------------


class SendStatus(StrEnum):
    """The subset of SPEC §5's ``message.status`` an adapter can report itself.

    Delivery and read receipts arrive later, as inbound ``delivery_status``
    events, so an adapter only ever returns one of these two.
    """

    SENT = "sent"
    FAILED = "failed"


@dataclass(frozen=True)
class SendResult:
    """What :meth:`Adapter.send` returns.

    ``error`` is a machine-readable code, not a sentence: it ends up on the
    message row and in the inbox, and a provider's error text routinely quotes
    the request — including credentials (SECURITY-BASELINE §5).
    """

    status: SendStatus
    provider_message_id: str = ""
    error: str = ""
