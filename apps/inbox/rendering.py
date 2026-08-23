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

**Media identifiers are the other kind of block.** A ``media`` block holds no
URL at all — it holds the id the platform gave us, because fetching it needs
that connection's credentials (``apps.channels.events.EventPayload`` draws the
line, ``apps.channels.media`` does the resolving). What this module puts in the
view model is a link to :func:`apps.inbox.views.media`, our own workspace-scoped
route, so the address in the DOM is one we minted and not one a stranger chose —
which is why :func:`is_renderable_url` is not consulted for it, and must not be:
the function answers a question about somebody else's string.

The block carries ``media_kind`` — what the platform *called* it — and that
decides the tag, exactly as it does for a ``file``/``audio``/``video`` block: an
image (or a platform that did not say) becomes a :class:`Media` part rendered as
an ``<img>``, and anything else becomes the same :class:`Link` part every other
non-image attachment already uses. Without it every voice note and PDF rendered
as a broken image icon.

Using the platform's word here is **not** in tension with SECURITY-BASELINE §9's
"sniff, do not trust". The two answer different questions: this one picks a tag
before any bytes exist, while §9 governs the ``Content-Type`` and the
disposition on the bytes themselves, which :mod:`apps.channels.media` still
decides by reading them. A platform that lies about the kind gets a link where a
picture would have been, or a broken ``<img>`` — never an inline render of
something that should have been an attachment.
"""

from dataclasses import dataclass
from typing import Any

from django.urls import NoReverseMatch, reverse
from django.utils.timesince import timeuntil

from apps.common.validators import is_renderable_url, is_valid_hex_color
from apps.inbox.codes import describe_inbox_failure
from apps.inbox.models import DEFAULT_LABEL_COLOR
from apps.messaging.codes import describe
from apps.messaging.models import Message, MessageDirection, MessageStatus

__all__ = [
    "DELETED_PREVIEW",
    "DELETED_REASON",
    "Button",
    "Card",
    "FailedScheduledReply",
    "Gallery",
    "Image",
    "LabelChip",
    "is_redacted",
    "Link",
    "Media",
    "PendingReminder",
    "PendingScheduledReply",
    "RenderedMessage",
    "RuleSummary",
    "Text",
    "Tombstone",
    "label_chip",
    "label_chips",
    "preview_of",
    "preview_of_body",
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

    @property
    def default_alt(self) -> str:
        """The accessible name when there is no caption.

        Here rather than in the template because it is copy, like
        ``Tombstone.reason`` — and because :class:`Media` shares the template
        branch while being able to say strictly less about itself. Merging the
        two branches without this quietly demoted every image's alt text to the
        weaker of the two.
        """
        return "Attached image"


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
class Media:
    """Media held as an identifier, pointed at our own resolution route.

    ``url`` is a path this application reversed, never anything from the
    payload. Only produced for media the platform called an image, or did not
    name at all — a known ``audio``/``video``/``file`` becomes a :class:`Link`.
    """

    url: str
    caption: str = ""
    kind: str = "media"

    @property
    def default_alt(self) -> str:
        """Deliberately vaguer than :attr:`Image.default_alt`.

        This part is produced both for a declared image and for media no
        platform named, and an alt that promised "image" for the second case
        would be describing a guess.
        """
        return "Attachment"


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


@dataclass(frozen=True)
class LabelChip:
    """One label as the reader sees it.

    ``color`` is the reason this is a view model rather than the row itself.
    ``style-src`` carries ``'unsafe-inline'`` (``config/settings/base.py``) and
    the conversation list already uses inline ``style`` attributes, and Django's
    ``escape()`` covers ``& < > " '`` and **not** ``;``, ``:`` or ``(`` — so a
    stored value of ``#fff;background-image:url(https://…`` would be a CSS
    beacon that never leaves the quotes, on a page where ``img-src`` allows
    ``https:``.

    So the hex is vetted **again** here, at render time, and an unrecognised one
    becomes the default rather than reaching the DOM.
    :func:`apps.common.validators.validate_hex_color` on the model is the write
    half; a row can still arrive through a migration, a data import or a shell.
    That is the same two-sided discipline :func:`_media` applies to a URL, and
    the fallback is the same idea as a :class:`Tombstone`: show something
    honest rather than nothing.
    """

    name: str
    color: str
    id: str = ""


@dataclass(frozen=True)
class PendingReminder:
    """A reminder waiting to fire, as the thread shows it.

    ``due_in`` is a rendered relative string ("in 20 minutes"), and it is in
    here rather than computed in the template because the thread's ETag hashes
    it — a countdown the token could not see would freeze on an open tab.
    """

    id: str
    who: str
    note: str
    due_at: Any
    due_in: str
    is_mine: bool = False


@dataclass(frozen=True)
class PendingScheduledReply:
    """A reply waiting to go out, as the thread shows it."""

    id: str
    preview: str
    due_at: Any
    due_in: str


@dataclass(frozen=True)
class FailedScheduledReply:
    """One that came due and was refused.

    ``reason`` is ``apps.messaging.codes.describe``'s sentence for a
    machine-readable code, never a provider's own text: those quote the request
    that caused them, credentials included (SECURITY-BASELINE §5).
    """

    id: str
    preview: str
    due_at: Any
    reason: str


@dataclass(frozen=True)
class RuleSummary:
    """A rule as the settings list describes it.

    Both tuples are **assembled from this workspace's own vocabulary** — label
    names, member names, platform labels — rather than serialised from the
    stored document. A rule's condition json is operator-authored and can name
    ids that no longer resolve; printing it raw would put unvetted text on the
    page and would also be unreadable.
    """

    id: str
    name: str
    enabled: bool
    priority: int
    conditions: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()


#: Anything :func:`render_message` can put in ``parts``.
type Part = Text | Image | Link | Media | Card | Gallery | Tombstone


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
    # The index is load-bearing, not decoration: a ``media`` block's delivery URL
    # addresses it by position in this list, which is how the view reads the id
    # back out of the row instead of taking it from the request.
    for index, item in enumerate(raw_blocks if isinstance(raw_blocks, list) else []):
        parts.append(_part(item, index, message))
    if is_redacted(message, body):
        # Its own reason, rather than the generic empty-body one below. "This
        # message has no displayable content" is what a reader sees for a
        # payload we could not parse, and telling that apart from "they unsent
        # it" is the difference between a bug report and an explanation.
        #
        # Replaces the parts rather than appending to them, so a retracted
        # message shows nothing it used to carry — including any ``media`` block
        # whose delivery URL was built a few lines above. ``apps.inbox.views``
        # refuses the same rows at the route, because a URL a reader already has
        # is not withdrawn by rendering it differently.
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


def is_redacted(message: Message, body: dict[str, Any]) -> bool:
    """Whether this row was retracted at the platform's request.

    Public because two places must agree on it: this module, which renders the
    tombstone, and :func:`apps.inbox.views.media`, which must refuse to resolve
    a retracted row's attachments. Rendering a message differently does not
    withdraw a URL a reader already has.

    Either signal is enough. The status is what ``apps.messaging.ingest`` writes
    and what the inbox filters on; the ``deleted`` marker inside the body is what
    a GDPR export or a hand-repaired row carries. Requiring both would mean a row
    that lost one of them renders its (empty) content as if nothing had happened.
    """
    return message.status == MessageStatus.DELETED or body.get("deleted") is True


def preview_of(message: Message) -> str:
    """One line for the conversation list.

    The redaction check needs the row — ``is_redacted`` reads the status as well
    as the body marker — so it stays here, and the body walk below is shared.
    """
    body = message.body if isinstance(message.body, dict) else {}
    if is_redacted(message, body):
        return DELETED_PREVIEW
    return preview_of_body(body)


def preview_of_body(raw: Any) -> str:
    """One line for any stored message body.

    Text if there is any, otherwise a description of what the message is. Both
    are escaped by the template like everything else; the truncation is about
    payload size and layout, not safety.

    Split out from :func:`preview_of` for the scheduled-reply card, which has a
    body but no ``Message`` row yet — the alternative was a second walker over
    the same shape, and there are already two in this file with different
    contracts (:func:`render_message` accounts for what it cannot show;
    ``apps.messaging.rendering.outbound_from_body`` drops it).
    """
    body = raw if isinstance(raw, dict) else {}
    if body.get("deleted") is True:
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
        elif not fallback and kind == "media":
            # What the platform called it, when it said — the same label a
            # platform-hosted attachment of that kind would get, so a list row
            # does not read differently depending on which field carried it.
            declared = _text(item.get("media_kind"))
            fallback = f"[{declared}]" if declared in _MEDIA_KINDS else "[attachment]"
        elif not fallback and (kind in _MEDIA_KINDS or kind in ("card", "gallery")):
            fallback = f"[{kind}]"
    return fallback


def _part(item: Any, index: int, message: Message) -> Part:
    if not isinstance(item, dict):
        return Tombstone(reason="Unreadable content.")
    kind = _text(item.get("type"))
    if kind == "text":
        text = _text(item.get("text"))
        return Text(text=text) if text else Tombstone(reason="Empty message.")
    if kind == "media":
        return _media_ref(item, index, message)
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


def _media_ref(item: dict[str, Any], index: int, message: Message) -> Part:
    """A ``media`` block as a link to this deployment's own resolution route.

    The id itself never reaches the URL — only the row and the block's position
    do, so the view reads it back out of stored, already-verified data. That is
    what keeps :func:`apps.channels.media.fetch_media` from becoming a way to
    ask a connection's credentials for an arbitrary identifier.

    A known non-image kind becomes an ordinary :class:`Link`, the same part a
    platform-hosted audio file or document already produces — one tag decision,
    written once, rather than a second vocabulary for the proxied case.
    """
    if not _text(item.get("media_id")):
        return Tombstone(reason="An attachment was recorded without an identifier.")
    try:
        url = reverse(
            "inbox:media",
            kwargs={
                "workspace_id": message.workspace_id,
                "conversation_id": message.conversation_id,
                "message_id": message.pk,
                "index": index,
            },
        )
    except NoReverseMatch:
        # Unreachable while the route exists, and a tombstone rather than a
        # raise regardless: this module's contract is that a thread renders.
        return Tombstone(reason="An attachment could not be linked.")

    caption = _text(item.get("caption"))
    kind = _text(item.get("media_kind"))
    if kind in _MEDIA_KINDS and kind != "image":
        return Link(url=url, media_kind=kind, caption=caption)
    # "image", and also "" — a platform that did not say. An <img> is the right
    # bet for the unknown case: it is what the overwhelming majority of inbound
    # media is, and when it loses the alt text shows inside a link that still
    # downloads the file.
    return Media(url=url, caption=caption)


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


# ---------------------------------------------------------------------------
# Labels and deferred work
# ---------------------------------------------------------------------------


def label_chip(label: Any) -> LabelChip:
    """One label, with its colour vetted. See :class:`LabelChip`."""
    color = getattr(label, "color", "") or ""
    return LabelChip(
        id=str(getattr(label, "pk", "") or ""),
        name=getattr(label, "name", "") or "",
        # Not ``or DEFAULT`` on the raw value: an empty string and "#nope" must
        # take the same path, and only one of them is falsy.
        color=color if is_valid_hex_color(color) and color else DEFAULT_LABEL_COLOR,
    )


def label_chips(labels: Any) -> tuple[LabelChip, ...]:
    """A row's chips, in the order they will print."""
    return tuple(label_chip(label) for label in labels)


def pending_reminder(reminder: Any, *, viewer: Any = None) -> PendingReminder:
    """A waiting reminder, with its countdown already rendered."""
    recipient = getattr(reminder, "recipient", None)
    return PendingReminder(
        id=str(reminder.pk),
        who=_display_name(recipient),
        note=reminder.note or "",
        due_at=reminder.remind_at,
        due_in=timeuntil(reminder.remind_at),
        is_mine=bool(viewer is not None and recipient is not None and recipient.pk == viewer.pk),
    )


def pending_scheduled_reply(reply: Any) -> PendingScheduledReply:
    """A queued reply, previewed with the same one-liner the list uses."""
    return PendingScheduledReply(
        id=str(reply.pk),
        preview=preview_of_body(reply.body),
        due_at=reply.send_at,
        due_in=timeuntil(reply.send_at),
    )


def failed_scheduled_reply(reply: Any) -> FailedScheduledReply:
    """One that was refused when it came due."""
    return FailedScheduledReply(
        id=str(reply.pk),
        preview=preview_of_body(reply.body),
        due_at=reply.send_at,
        reason=describe_inbox_failure(reply.error) if reply.error else "It could not be sent.",
    )


def rule_summary(rule: Any, *, labels: Any, members: Any, connections: Any) -> RuleSummary:
    """Describe a rule in words, from names this workspace owns.

    ``labels``, ``members`` and ``connections`` are id → name maps the caller
    built once for the whole page; resolving them per rule would be three
    queries per row on a list that exists to be scanned.

    An id that no longer resolves becomes a visible placeholder rather than
    disappearing — the :class:`Tombstone` argument again. A rule quietly
    describing itself as doing less than it does is how an operator ends up
    debugging automation that is not in front of them.
    """
    from apps.common.platforms import Platform

    document = rule.condition_json if isinstance(rule.condition_json, dict) else {}
    raw_channel = document.get("channel")
    channel: dict[str, Any] = raw_channel if isinstance(raw_channel, dict) else {}
    conditions: list[str] = []

    platform_labels = dict(Platform.choices)
    platforms = [str(platform_labels.get(value, value)) for value in channel.get("platforms") or []]
    if platforms:
        conditions.append("Channel is " + _joined(platforms))
    names = [connections.get(str(value), _MISSING) for value in channel.get("connection_ids") or []]
    if names:
        conditions.append("Connection is " + _joined(names))

    words = [str(item.get("text", "")) for item in document.get("keywords") or [] if isinstance(item, dict)]
    if words:
        conditions.append("Message mentions " + _joined(words))

    contact = document.get("contact")
    if isinstance(contact, dict) and contact.get("rules"):
        count = len(contact["rules"])
        mode = "all" if contact.get("match", "all") == "all" else "any"
        conditions.append(f"Contact matches {mode} of {count} condition{'' if count == 1 else 's'}")

    actions: list[str] = []
    for item in rule.actions_json if isinstance(rule.actions_json, list) else []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "add_label":
            actions.append("Add label " + labels.get(str(item.get("label_id")), _MISSING))
        elif kind == "assign_to_member":
            actions.append("Assign to " + members.get(str(item.get("user_id")), _MISSING))
        elif kind == "mark_done":
            actions.append("Mark done")

    return RuleSummary(
        id=str(rule.pk),
        name=rule.name,
        enabled=rule.enabled,
        priority=rule.priority,
        conditions=tuple(conditions),
        actions=tuple(actions),
    )


#: What a rule prints for an id that no longer resolves. Copy, like
#: :attr:`Tombstone.reason`, and never the id itself.
_MISSING = "(deleted)"


def _joined(values: list[str]) -> str:
    """ "a, b or c" — the reader's conjunction, since every list here is an OR."""
    if len(values) == 1:
        return values[0]
    return ", ".join(values[:-1]) + " or " + values[-1]


def _display_name(user: Any) -> str:
    if user is None:
        return "someone who has left"
    return getattr(user, "display_name", "") or getattr(user, "email", "") or "a teammate"
