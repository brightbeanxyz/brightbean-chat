"""SPEC §11.1 — render one abstract message and hand it to contract 1.

    Behavior: renders per capability flags; if buttons/QRs present -> Wait, else
    Continue(default).

The node renders **once**, abstractly, into an ``OutboundMessage``; the adapter
(via ``apps.channels.downgrade``) is what turns that into whatever the platform
can carry. Nothing here branches on a platform, and nothing here builds a
provider payload.

Three rules do the work, and each one is somebody else's decision this node is
obeying:

* **Every author string goes through the shared renderer** (SECURITY-BASELINE
  §3). Block text, captions, card titles, button labels, URLs — all of it, via
  ``ctx.render``, which is plain token substitution with no template engine
  anywhere near it.
* **Media resolves through the library**, ``media_library.resolution.resolve``
  with its required ``workspace`` kwarg. That module's docstring already decided
  what a missing asset means: "a deleted image stops the message, not the flow."
* **A failed send follows ``default``** (SPEC §9.5). The message row carries the
  reason; the run continues. It does *not* then wait for buttons the contact
  never saw — a wait for a reply to a message that was never delivered is a
  contact parked until the 30-day sweep.
"""

import logging
from typing import Any

from apps.channels.events import Button, Card, CardBlock, GalleryBlock, MediaBlock, OutboundMessage, QuickReply
from apps.channels.events import TextBlock as OutboundText
from apps.flows.engine.context import NodeContext
from apps.flows.engine.nodes.base import Node
from apps.flows.engine.registry import register_node
from apps.flows.engine.results import Continue, Fail, StepResult, Wait
from apps.flows.engine.sending import deliver
from apps.flows.engine.waits import buttons_wait
from apps.flows.messaging import FacadeUnavailableError
from apps.media_library.resolution import MediaNotFoundError, resolve

__all__ = ["SendMessageNode"]

logger = logging.getLogger(__name__)

_MEDIA_KINDS = ("image", "audio", "video", "file")


class _UnresolvableMediaError(Exception):
    """A block names an asset this workspace cannot read. Stops the message."""


@register_node
class SendMessageNode(Node):
    """Send a message; wait for a reply when the message asks for one."""

    type = "send_message"
    synchronous_safe = True

    def execute(self, ctx: NodeContext) -> StepResult:
        buttons = _buttons(ctx)
        quick_replies = _quick_replies(ctx)

        try:
            blocks = _blocks(ctx)
        except _UnresolvableMediaError as exc:
            logger.warning("Execution %s: node %s not sent — %s", ctx.execution.pk, ctx.node_id, exc)
            return Continue("default")

        if not blocks:
            # The schema requires at least one block, so this is a draft or a
            # hand-edited graph. Sending an empty message would be an API error
            # per platform; skipping is the quieter equivalent of a failed send.
            logger.warning("Execution %s: node %s has nothing to send.", ctx.execution.pk, ctx.node_id)
            return Continue("default")

        outbound = OutboundMessage(blocks=tuple(blocks), buttons=tuple(buttons), quick_replies=tuple(quick_replies))
        try:
            outcome = deliver(ctx.execution, outbound, node_id=ctx.node_id)
        except FacadeUnavailableError as exc:
            # A deployment problem rather than a flow problem, and one no retry
            # fixes, so it is a named failure rather than a silent skip.
            return Fail(f"send_message node {ctx.node_id}: {exc}")

        if not outcome.sent:
            return Continue("default")

        if buttons or quick_replies:
            return Wait(
                buttons_wait(
                    ctx.node_id,
                    buttons=ctx.config.get("buttons"),
                    quick_replies=ctx.config.get("quick_replies"),
                    followup=ctx.config.get("followup"),
                    retry_unmatched=ctx.config.get("retry_unmatched"),
                    labels=_labels(buttons, quick_replies),
                )
            )
        return Continue("default")


# ---------------------------------------------------------------------------
# Rendering the config into channel-shaped blocks
# ---------------------------------------------------------------------------


def _blocks(ctx: NodeContext) -> list[Any]:
    rendered: list[Any] = []
    for block in ctx.config.get("blocks") or []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            text = ctx.render(block.get("text"))
            if text:
                rendered.append(OutboundText(text=text))
        elif kind in _MEDIA_KINDS:
            rendered.append(
                MediaBlock(kind=str(kind), url=_media_url(ctx, block), caption=ctx.render(block.get("caption")))
            )
        elif kind == "card":
            rendered.append(CardBlock(card=_card(ctx, block)))
        elif kind == "gallery":
            cards = [_card(ctx, card) for card in block.get("cards") or [] if isinstance(card, dict)]
            if cards:
                rendered.append(GalleryBlock(cards=tuple(cards)))
    return rendered


def _card(ctx: NodeContext, card: dict[str, Any]) -> Card:
    """SPEC §11.1's card — the same four fields alone or inside a gallery."""
    url_button = card.get("url_button") if isinstance(card.get("url_button"), dict) else {}
    buttons: tuple[Button, ...] = ()
    if url_button:
        buttons = (
            Button(
                # A card's URL button has no id in the schema and never comes
                # back as an event, so a stable synthetic one is enough for the
                # adapter to render it.
                id="url",
                label=ctx.render(url_button.get("label")),
                url=ctx.render(url_button.get("url")),
            ),
        )
    return Card(
        title=ctx.render(card.get("title")),
        subtitle=ctx.render(card.get("subtitle")),
        image_url=_media_url(ctx, card, key="image"),
        url=ctx.render(url_button.get("url")) if url_button else "",
        buttons=buttons,
    )


def _media_url(ctx: NodeContext, block: dict[str, Any], key: str = "media_id") -> str:
    """A deliverable URL for one media reference — library id or plain URL.

    SPEC §11.1 allows either. A library id is resolved to a signed delivery URL
    at send time, so a block stores a stable id and the URL is minted fresh from
    whatever storage the deployment runs.

    A card's ``image`` is one field carrying both forms, which is why ``key``
    exists: it is tried as an id first and falls back to being rendered as a URL.
    """
    reference = block.get(key)
    if isinstance(reference, str) and reference:
        try:
            return str(resolve(reference, workspace=ctx.workspace)["url"])
        except MediaNotFoundError:
            if key == "media_id":
                # An explicit media_id that does not resolve is a deleted asset,
                # not a URL. The library's own contract: stop the message.
                raise _UnresolvableMediaError(f"media {reference!r} is not in this workspace's library") from None
    url = ctx.render(block.get("url") if key == "media_id" else reference)
    if not url and key == "media_id":
        raise _UnresolvableMediaError("a media block carries neither a library id nor a URL")
    return url


def _buttons(ctx: NodeContext) -> list[Button]:
    buttons = []
    for button in ctx.config.get("buttons") or []:
        if not isinstance(button, dict) or not isinstance(button.get("id"), str):
            continue
        buttons.append(
            Button(
                id=button["id"],
                label=ctx.render(button.get("label")),
                url=ctx.render(button.get("url")) if button.get("action") == "url" else "",
            )
        )
    return buttons


def _quick_replies(ctx: NodeContext) -> list[QuickReply]:
    return [
        QuickReply(id=reply["id"], label=ctx.render(reply.get("label")))
        for reply in ctx.config.get("quick_replies") or []
        if isinstance(reply, dict) and isinstance(reply.get("id"), str)
    ]


def _labels(buttons: list[Button], quick_replies: list[QuickReply]) -> dict[str, str]:
    """Rendered label -> id, for platforms that reply with text (SPEC §6.2).

    Built from the *rendered* labels rather than the config, so a quick reply
    reading "Yes, {{first_name}}" matches what the contact was actually shown.
    URL buttons are excluded: they open a link and never come back.
    """
    labels = {reply.label: reply.id for reply in quick_replies if reply.label}
    labels.update({button.label: button.id for button in buttons if button.label and not button.is_url})
    return labels
