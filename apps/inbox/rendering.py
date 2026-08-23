"""Turning an attacker-authored ``Message.body`` into something safe to render.

This is the sharp end of the inbox. SECURITY-BASELINE §2: message text,
usernames, profile fields and media URLs are all attacker-controlled, and a
thread is the one place in the product where a stranger's bytes reach a team
member's browser. Every block here arrived over a webhook.

The discipline is:

* **No HTML is produced in this module.** It returns plain view models and the
  templates render them under Django's default autoescaping. Nothing in
  ``apps.inbox`` calls ``mark_safe`` or ``format_html``, and
  ``tests/test_hostile_content.py`` scans the package's AST to keep it that way
  — the same assertion ``apps.messaging`` makes about itself.
* **A URL is checked before it can become an ``href`` or a ``src``**, with
  :func:`apps.common.validators.is_renderable_url`. ``apps.messaging.ingest``
  stores attachment URLs verbatim (it caps length and strips NULs, and
  deliberately does not look at the scheme), so ``javascript:alert(1)`` is a
  value that really can be in a body.
* **Anything that cannot be shown safely becomes a tombstone**, not silence.
  ``apps.messaging.rendering.outbound_from_body`` drops what it does not
  recognise, which is right for a retry — sending less beats sending something
  wrong — but wrong for a reader, who would be looking at a thread with a hole
  in it and no way to know. A rejected URL, an unrecognised block type and an
  empty body all render as a visible marker instead.

Which HTML tag a media block earns is decided here rather than in the template,
because the answer comes from the Content-Security-Policy in
``config/settings/base.py``: ``img-src`` allows ``https:``, so an image is an
``<img>``; ``media-src`` is ``'self' blob:`` only, so an ``<audio>`` or
``<video>`` pointing at a platform CDN would be blocked by the browser and
render as a broken control. Those become labelled links.
"""

from dataclasses import dataclass
from typing import Any

from apps.common.validators import is_renderable_url
from apps.messaging.codes import describe
from apps.messaging.models import Message, MessageDirection, MessageStatus

__all__ = [
    "DELETED_PREVIEW",
    "DELETED_REASON",
    "Button",
    "Card",
    "Gallery",
    "Image",
    "Link",
    "RenderedMessage",
    "Text",
    "Tombstone",
    "preview_of",
    "render_message",
]

#: Media kinds ``apps.channels.events.MediaBlock`` can carry.
_MEDIA_KINDS = frozenset({"image", "audio", "video", "file"})

#: What a retracted message says in the thread and in the list. Copy, written
#: here, never anything derived from the payload — there is no payload left.
DELETED_REASON = "This message was deleted."
DELETED_PREVIEW = "[deleted]"

#: How much of a message the conversation list shows. Inbound text is capped at
#: ``ingest.MAX_TEXT_CHARS`` (100k), so a list of a hundred rows would otherwise
#: be able to weigh ten megabytes.
PREVIEW_CHARS = 140


@dataclass(frozen=True)
class Text:
    text: str
    kind: str = "text"


@dataclass(frozen=True)
class Image:
    """An image block whose URL passed the scheme check."""

    url: str
    caption: str = ""
    kind: str = "image"


@dataclass(frozen=True)
class Link:
    """Audio, video or a file: a labelled link rather than an inline player.

    ``media_kind`` is what the platform called it, for the icon and the label.
    """

    url: str
    media_kind: str
    caption: str = ""
    kind: str = "link"


@dataclass(frozen=True)
class Button:
    """A button as the reader sees it.

    ``url`` is empty for a postback — there is nothing for a team member to
    click, because the target was the contact's app, so it renders as an inert
    chip rather than a dead link.
    """

    label: str
    url: str = ""


@dataclass(frozen=True)
class Card:
    title: str = ""
    subtitle: str = ""
    image_url: str = ""
    url: str = ""
    buttons: tuple[Button, ...] = ()
    kind: str = "card"


@dataclass(frozen=True)
class Gallery:
    cards: tuple[Card, ...] = ()
    kind: str = "gallery"


@dataclass(frozen=True)
class Tombstone:
    """Content that exists but cannot be shown.

    ``reason`` is copy written here, never anything derived from the payload.
    """

    reason: str
    kind: str = "tombstone"


#: Anything :func:`render_message` can put in ``parts``.
type Part = Text | Image | Link | Card | Gallery | Tombstone


@dataclass(frozen=True)
class RenderedMessage:
    """One bubble's worth of already-vetted values."""

    message: Message
    parts: tuple[Part, ...] = ()
    buttons: tuple[Button, ...] = ()
    quick_replies: tuple[Button, ...] = ()
    #: The postback id or deep-link ref an inbound event carried, if any.
    button_id: str = ""
    ref: str = ""
    reason: str = ""

    @property
    def is_inbound(self) -> bool:
        return self.message.direction == MessageDirection.IN

    @property
    def is_note(self) -> bool:
        return bool(self.message.internal)

    @property
    def is_failed(self) -> bool:
        return self.message.status == MessageStatus.FAILED

    @property
    def is_deleted(self) -> bool:
        """Retracted at the platform's request (SPEC §6.3, §19).

        The row is kept and its body redacted by ``apps.messaging.ingest``, so
        the thread keeps its shape and an agent can see that something was here
        and is not any more — which is the whole point of redacting rather than
        deleting.
        """
        return self.message.status == MessageStatus.DELETED

    @property
    def can_retry(self) -> bool:
        """A failed outbound send a human can ask for again.

        Never a note (nothing was ever sent) and never an inbound row (there is
        nothing on our side to repeat).
        """
        return self.is_failed and not self.is_note and not self.is_inbound


def render_message(message: Message) -> RenderedMessage:
    """Vet ``message.body`` and return what the template may show.

    Walks the raw json rather than going through
    :func:`apps.messaging.rendering.outbound_from_body`, because that function's
    contract is to drop what it does not recognise and this one's is to account
    for it.
    """
    body = message.body if isinstance(message.body, dict) else {}
    raw_blocks = body.get("blocks")
    parts: list[Part] = []
    for item in raw_blocks if isinstance(raw_blocks, list) else []:
        parts.append(_part(item))
    if _is_redacted(message, body):
        # Its own reason, rather than the generic empty-body one below. "This
        # message has no displayable content" is what a reader sees for a
        # payload we could not parse, and telling that apart from "they unsent
        # it" is the difference between a bug report and an explanation.
        parts = [Tombstone(reason=DELETED_REASON)]
    elif not parts:
        parts.append(Tombstone(reason="This message has no displayable content."))
    return RenderedMessage(
        message=message,
        parts=tuple(parts),
        buttons=_buttons(body.get("buttons")),
        quick_replies=_buttons(body.get("quick_replies")),
        button_id=_text(body.get("button_id")),
        ref=_text(body.get("ref")),
        reason=describe(message.error),
    )


def _is_redacted(message: Message, body: dict[str, Any]) -> bool:
    """Whether this row was retracted at the platform's request.

    Either signal is enough. The status is what ``apps.messaging.ingest`` writes
    and what the inbox filters on; the ``deleted`` marker inside the body is what
    a GDPR export or a hand-repaired row carries. Requiring both would mean a row
    that lost one of them renders its (empty) content as if nothing had happened.
    """
    return message.status == MessageStatus.DELETED or body.get("deleted") is True


def preview_of(message: Message) -> str:
    """One line for the conversation list.

    Text if there is any, otherwise a description of what the message is. Both
    are escaped by the template like everything else; the truncation is about
    payload size and layout, not safety.
    """
    body = message.body if isinstance(message.body, dict) else {}
    if _is_redacted(message, body):
        return DELETED_PREVIEW
    raw_blocks = body.get("blocks")
    # One pass, remembering the best non-text answer seen. Text wins wherever it
    # appears in the block list, so a second full traversal would only be there
    # to find what this one already walked past.
    fallback = ""
    for item in raw_blocks if isinstance(raw_blocks, list) else []:
        if not isinstance(item, dict):
            continue
        kind = _text(item.get("type"))
        if kind == "text":
            text = " ".join(_text(item.get("text")).split())
            if text:
                return text[:PREVIEW_CHARS]
        elif not fallback and (kind in _MEDIA_KINDS or kind in ("card", "gallery")):
            fallback = f"[{kind}]"
    return fallback


def _part(item: Any) -> Part:
    if not isinstance(item, dict):
        return Tombstone(reason="Unreadable content.")
    kind = _text(item.get("type"))
    if kind == "text":
        text = _text(item.get("text"))
        return Text(text=text) if text else Tombstone(reason="Empty message.")
    if kind in _MEDIA_KINDS:
        return _media(kind, item)
    if kind == "card":
        return Card(**_card_kwargs(item))
    if kind == "gallery":
        cards = item.get("cards")
        rendered = (
            tuple(Card(**_card_kwargs(card)) for card in cards if isinstance(card, dict))
            if isinstance(cards, list)
            else ()
        )
        return Gallery(cards=rendered) if rendered else Tombstone(reason="Empty gallery.")
    return Tombstone(reason="Unsupported content.")


def _media(kind: str, item: dict[str, Any]) -> Part:
    url = _text(item.get("url"))
    caption = _text(item.get("caption"))
    if not is_renderable_url(url):
        # Deliberately does not echo the URL: the reason line is copy, and the
        # rejected value is the payload we just refused to hand the browser.
        return Tombstone(reason=f"An attachment was hidden because its address is not a web link ({kind}).")
    if kind == "image":
        return Image(url=url, caption=caption)
    return Link(url=url, media_kind=kind, caption=caption)


def _card_kwargs(item: dict[str, Any]) -> dict[str, Any]:
    image_url = _text(item.get("image_url"))
    url = _text(item.get("url"))
    return {
        "title": _text(item.get("title")),
        "subtitle": _text(item.get("subtitle")),
        "image_url": image_url if is_renderable_url(image_url) else "",
        "url": url if is_renderable_url(url) else "",
        "buttons": _buttons(item.get("buttons")),
    }


def _buttons(value: Any) -> tuple[Button, ...]:
    if not isinstance(value, list):
        return ()
    out: list[Button] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        label = _text(item.get("label"))
        url = _text(item.get("url"))
        out.append(Button(label=label, url=url if is_renderable_url(url) else ""))
    return tuple(out)


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""
