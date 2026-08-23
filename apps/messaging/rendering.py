"""Reading an ``OutboundMessage`` back out of a stored ``Message.body``.

A retry happens minutes or hours after the send it is retrying, in a different
process, with the caller long gone. The row is the only thing left, so the row
has to be enough — which makes this the inverse of
:meth:`apps.channels.events.OutboundMessage.to_body`, whose docstring already
calls that json "a persisted contract".

It lives here rather than in ``apps.channels`` on purpose. ``to_body`` serves
every consumer of the event vocabulary; the *inverse* is needed by exactly one
of them, the retry path, and putting it beside its caller keeps the channels
app from growing an API for a Layer-3 concern.

Defensive throughout. The body it reads was written by an older release, or
hand-edited in the admin, or is simply a shape a later block type introduced;
none of those should raise on a retry path. An unrecognised block is dropped
rather than guessed at, which sends less than intended — visible in the thread —
instead of sending something wrong.
"""

from typing import Any

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

__all__ = ["outbound_from_body"]

_MEDIA_KINDS = frozenset({"image", "audio", "video", "file"})


def outbound_from_body(body: Any) -> OutboundMessage:
    """Rebuild the message a stored body describes."""
    if not isinstance(body, dict):
        return OutboundMessage()
    return OutboundMessage(
        blocks=tuple(filter(None, (_block(item) for item in _list(body.get("blocks"))))),
        buttons=tuple(filter(None, (_button(item) for item in _list(body.get("buttons"))))),
        quick_replies=tuple(filter(None, (_quick_reply(item) for item in _list(body.get("quick_replies"))))),
        tag=_text(body.get("tag")) or None,
        template_ref=_text(body.get("template_ref")) or None,
        # Absent from every row written before issue #12, which reads back as
        # "" — the same thing an agent reply or an API send stores, and what an
        # adapter already has to handle as "no node behind this message".
        node_id=_text(body.get("node_id")),
        # Same story one issue later (#21): absent from every row written
        # before the email channel, and "" is already what every non-email
        # send stores, so a retry of an older row rebuilds unchanged.
        subject=_text(body.get("subject")),
        from_override=_text(body.get("from_override")),
        html_body=_text(body.get("html_body")),
    )


def _list(value: Any) -> list[Any]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _block(item: dict[str, Any]) -> Any:
    kind = _text(item.get("type"))
    if kind == "text":
        text = _text(item.get("text"))
        return TextBlock(text=text) if text else None
    if kind in _MEDIA_KINDS:
        url = _text(item.get("url"))
        return MediaBlock(kind=kind, url=url, caption=_text(item.get("caption"))) if url else None
    if kind == "card":
        return CardBlock(card=_card(item))
    if kind == "gallery":
        cards = tuple(_card(card) for card in _list(item.get("cards")))
        return GalleryBlock(cards=cards) if cards else None
    return None


def _card(item: dict[str, Any]) -> Card:
    return Card(
        title=_text(item.get("title")),
        subtitle=_text(item.get("subtitle")),
        image_url=_text(item.get("image_url")),
        url=_text(item.get("url")),
        buttons=tuple(filter(None, (_button(button) for button in _list(item.get("buttons"))))),
    )


def _button(item: dict[str, Any]) -> Button | None:
    identifier = _text(item.get("id"))
    label = _text(item.get("label"))
    # A button with no id cannot be matched when it comes back as a postback, so
    # it would be a control the flow engine can never resume from.
    return Button(id=identifier, label=label, url=_text(item.get("url"))) if identifier else None


def _quick_reply(item: dict[str, Any]) -> QuickReply | None:
    identifier = _text(item.get("id"))
    return QuickReply(id=identifier, label=_text(item.get("label"))) if identifier else None
