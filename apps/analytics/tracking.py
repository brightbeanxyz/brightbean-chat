"""Minting and reading the ``/c/`` and ``/o/`` tokens, and wrapping what carries them.

``apps/common/signing.py`` reserves both routes for this issue by name, and
``apps/channels/urls_public.py`` says the same. They join ``/u/`` unsubscribe and
``/m/`` media delivery in the public token-route family, and they follow
:mod:`apps.channels.unsubscribe` in every decision that family has already made:
one signing utility, a ``purpose`` salt so a token minted here cannot be replayed
against another route, short payload keys because the string is embedded in every
message, and ``max_age=None``.

**No expiry, deliberately.** A button sits in somebody's chat and a newsletter
sits in somebody's inbox for years. An expired click link is a broken link in a
message this deployment sent, which is worse than a counter that keeps counting.

**Signed is not secret.** The payload is readable by anyone holding the token —
that is what ``sign`` promises — so it carries a flow id, a node id and the
destination the author already published in the message. It carries **no contact
id**: SPEC §18 puts per-contact click history out of scope for v1, and a token
that named a recipient would be one forwarded email away from telling a stranger
who else was mailed.

--------------------------------------------------------------------------
The destination comes out of the payload
--------------------------------------------------------------------------

A click redirect is an open redirect if you let it be. The target is read from
the *verified* payload and never from the query string, and it is re-checked
against ``apps.common.validators.is_renderable_url`` — http/https with a host,
nothing else — at redirect time rather than only at mint time, so a token minted
before any future change to this module still cannot become one.

--------------------------------------------------------------------------
What gets wrapped
--------------------------------------------------------------------------

:func:`instrument` rewrites, for a node send that is not a preview:

* every **URL button**, on every platform (SPEC §18), including the ones inside
  a card or a gallery. A postback button has no URL and is left alone. Email has
  no button widget, so ``apps.channels.downgrade`` inlines these as ``label:
  url`` and ``email_html.sanitize`` linkifies them — the wrapper is already in
  the string by then, so email gets click tracking on buttons for free;
* **anchors inside an authored email body**, and only when the workspace has
  opted in (:class:`apps.analytics.models.TrackingSettings`);
* an appended **1×1 open pixel**, likewise opt-in.

The anchor rewrite is a narrow regex over double-quoted ``href`` values in ``<a>``
start tags, and its failure mode is deliberate: an exotic anchor it does not match
is simply *not counted*. It is not a security control — ``email_html.sanitize``
bounds the schemes on the way out and the ``/c/`` view checks again on the way
back — so a miss costs a number, never safety.

Media URLs are never wrapped. A ``MediaBlock``'s URL is an ``<img src>``, not
something a reader clicks, and pointing it at a redirect would break the image.

--------------------------------------------------------------------------
A wrapper that would not fit is not applied
--------------------------------------------------------------------------

Signing *expands* a URL — roughly 175 characters of fixed overhead plus base64
growth on anything that does not compress — and two limits downstream truncate
rather than refuse:

* ``apps.channels.downgrade`` inlines a URL button into the text for a platform
  with no button widget and then splits the text at the platform's
  ``max_text_len``. A 1 515-character target becomes a 1 729-character ``/c/``
  URL, which is past SMS's 1 600 and gets cut **mid-token** — a link that cannot
  work at all, where the raw one would have.
* ``email_html.sanitize`` parses only the first ``MAX_HTML_CHARS`` of a body, so
  an authored email already near that limit loses its tail to an anchor rewrite,
  or silently drops an appended pixel.

So both transformations are conditional on the result still fitting. An
untracked link is a missing number; a truncated one is a broken message, and the
first is the direction to fail in.
"""

import logging
import re
from dataclasses import dataclass, replace
from html import unescape
from typing import Any
from urllib.parse import urljoin

from django.conf import settings
from django.urls import reverse

from apps.channels.capabilities import capabilities_for
from apps.channels.events import Button, Card, CardBlock, GalleryBlock, OutboundMessage
from apps.channels.providers.email_html import MAX_HTML_CHARS
from apps.common.platforms import Platform
from apps.common.signing import sign, unsign_or_404
from apps.common.validators import is_renderable_url

logger = logging.getLogger(__name__)

__all__ = [
    "CLICK_PURPOSE",
    "OPEN_PURPOSE",
    "ClickTarget",
    "OpenTarget",
    "click_target_from_token",
    "click_url",
    "instrument",
    "open_target_from_token",
    "open_url",
]

#: The signer salts. Two, not one: a pixel token must not be replayable as a
#: redirect, and neither may stand in for an unsubscribe link.
CLICK_PURPOSE = "click-tracking"
OPEN_PURPOSE = "open-pixel"

#: Short keys — this string ends up inside every message, and for SMS it is
#: charged by the character.
FLOW_KEY = "f"
NODE_KEY = "n"
URL_KEY = "u"
WORKSPACE_KEY = "w"
MESSAGE_KEY = "k"

#: A target longer than this is left unwrapped rather than turned into a token
#: several times its own length. Matches the graph schema's own ``maxLength`` on
#: a button URL, so nothing the builder produces reaches it; a hand-edited graph
#: can. The per-platform budget below is the limit that actually binds.
MAX_TARGET_CHARS = 2000

#: The narrowest ``max_text_len`` in ``apps.channels.capabilities`` (Instagram's
#: 1 000). What an unrecognised platform is held to — see :func:`_text_budget`.
_SMALLEST_TEXT_BUDGET = 1000

#: Double-quoted ``href`` values inside an ``<a>`` start tag. ``[^>]*?`` cannot
#: cross into the following tag, so a malformed document costs a missed count
#: rather than a mangled one.
_ANCHOR_HREF_RE = re.compile(r'(<a\b[^>]*?\bhref\s*=\s*")([^"]*)(")', re.IGNORECASE)

#: The pixel, appended to an authored body. ``alt=""`` and the two dimensions are
#: all inside ``email_html.ALLOWED_ATTRIBUTES["img"]``, so it survives the
#: sanitiser the adapter runs on the way out.
_PIXEL_HTML = '<img src="{url}" width="1" height="1" alt="" />'


# ---------------------------------------------------------------------------
# Minting
# ---------------------------------------------------------------------------


def click_url(*, flow_id: Any, node_id: str, target: str) -> str:
    """The absolute ``/c/<token>/`` URL that stands in for ``target``.

    Built from ``settings.APP_URL`` rather than from a request, because the send
    path runs in a worker where there is no request — the same reasoning
    ``apps.channels.unsubscribe`` and ``apps.media_library.delivery`` give.
    """
    token = sign(
        {FLOW_KEY: str(flow_id), NODE_KEY: str(node_id), URL_KEY: str(target)},
        purpose=CLICK_PURPOSE,
    )
    return _absolute(reverse("click_redirect", kwargs={"token": token}))


def open_url(*, workspace_id: Any, idempotency_key: str) -> str:
    """The absolute ``/o/<token>/`` pixel URL for one message.

    The message row does not exist yet when this is minted — the body carrying
    the pixel is what the facade inserts — so the token names the message the
    only way anything can at that moment: by SPEC §9.4's idempotency key, which
    the caller has already computed and which the facade stores under a unique
    index.
    """
    token = sign(
        {WORKSPACE_KEY: str(workspace_id), MESSAGE_KEY: str(idempotency_key)},
        purpose=OPEN_PURPOSE,
    )
    return _absolute(reverse("open_pixel", kwargs={"token": token}))


def _absolute(path: str) -> str:
    return urljoin(settings.APP_URL.rstrip("/") + "/", path.lstrip("/"))


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClickTarget:
    """What a ``/c/`` token names."""

    flow_id: str
    node_id: str
    url: str


@dataclass(frozen=True)
class OpenTarget:
    """What an ``/o/`` token names."""

    workspace_id: str
    idempotency_key: str


def click_target_from_token(token: str) -> ClickTarget:
    """What a click token names, or ``Http404`` for every failure.

    ``max_age=None`` per the module docstring. Every rejection — bad signature,
    a token minted for the pixel or for unsubscribe, an unknown version, a
    malformed blob — comes back as one indistinguishable bare 404, constant-time
    underneath (SECURITY-BASELINE §4).
    """
    payload = unsign_or_404(token, purpose=CLICK_PURPOSE, max_age=None)
    return ClickTarget(
        flow_id=str(payload.get(FLOW_KEY) or ""),
        node_id=str(payload.get(NODE_KEY) or ""),
        url=str(payload.get(URL_KEY) or ""),
    )


def open_target_from_token(token: str) -> OpenTarget:
    """What a pixel token names, or ``Http404`` for every failure."""
    payload = unsign_or_404(token, purpose=OPEN_PURPOSE, max_age=None)
    return OpenTarget(
        workspace_id=str(payload.get(WORKSPACE_KEY) or ""),
        idempotency_key=str(payload.get(MESSAGE_KEY) or ""),
    )


# ---------------------------------------------------------------------------
# Wrapping
# ---------------------------------------------------------------------------


def instrument(
    outbound: OutboundMessage,
    *,
    execution: Any,
    node_id: str,
    platform: str,
    idempotency_key: str,
) -> OutboundMessage:
    """Return ``outbound`` with its links wrapped for tracking.

    Returns the message **unchanged** for a preview run. A preview's job is to
    check content, an unwrapped link is the more honest test of where a button
    goes, and it removes any way for a test click to reach a counter — which is
    the promise ``apps/flows/models.py``, ``apps/flows/engine/runner.py`` and
    ``apps/broadcasts/handlers.py`` all make about ``FlowExecution.preview``.
    """
    if getattr(execution, "preview", False) or not node_id:
        return outbound

    flow_id = getattr(execution, "flow_id", None)
    workspace_id = getattr(execution, "workspace_id", None)
    if flow_id is None or workspace_id is None:
        return outbound

    budget = _text_budget(platform)

    def wrap(url: str) -> str:
        return _wrapped(url, flow_id=flow_id, node_id=node_id, budget=budget)

    changed = replace(
        outbound,
        buttons=tuple(_button(button, wrap) for button in outbound.buttons),
        blocks=tuple(_block(block, wrap) for block in outbound.blocks),
    )

    if platform != Platform.EMAIL.value or not changed.html_body:
        return changed
    return _email_body(changed, workspace_id=workspace_id, wrap=wrap, idempotency_key=idempotency_key)


def _text_budget(platform: str) -> int:
    """The longest a wrapped URL may be on this platform.

    ``Capabilities.max_text_len`` is the length at which
    ``apps.channels.downgrade`` splits a message, and a URL button on a platform
    with no button widget is inlined into exactly that text. It is a ceiling
    rather than a layout budget — a URL filling the whole of it leaves no room
    for the message around it — but it is the number that decides whether the
    link survives at all, which is the question this answers.

    An unknown platform gets the smallest budget in the table rather than an
    unbounded one: a wrapper that might not fit should not be minted on the
    strength of a name nobody recognises.
    """
    try:
        return int(capabilities_for(platform).max_text_len)
    except (KeyError, ValueError, TypeError):
        return _SMALLEST_TEXT_BUDGET


def _wrapped(url: str, *, flow_id: Any, node_id: str, budget: int) -> str:
    """The ``/c/`` stand-in for one target, or the target itself if unwrappable.

    Three ways to decline, all of them returning the original: a non-http(s)
    target, one past :data:`MAX_TARGET_CHARS`, and — the one that actually
    happens — a signed URL too long for the platform to carry. See the module
    docstring on why an untracked link beats a truncated one.
    """
    if not is_renderable_url(url) or len(url) > MAX_TARGET_CHARS:
        return url
    wrapped = click_url(flow_id=flow_id, node_id=node_id, target=url)
    if len(wrapped) > budget:
        logger.info(
            "A tracking URL for node %s would be %s characters, past this platform's %s; left unwrapped.",
            node_id,
            len(wrapped),
            budget,
        )
        return url
    return wrapped


def _button(button: Button, wrap: Any) -> Button:
    """A URL button, wrapped. A postback button has no URL and is untouched."""
    if not button.is_url:
        return button
    return replace(button, url=wrap(button.url))


def _card(card: Card, wrap: Any) -> Card:
    return replace(
        card,
        url=wrap(card.url) if card.url else card.url,
        buttons=tuple(_button(button, wrap) for button in card.buttons),
    )


def _block(block: Any, wrap: Any) -> Any:
    """Cards and galleries carry buttons; text and media blocks do not."""
    if isinstance(block, CardBlock):
        return replace(block, card=_card(block.card, wrap))
    if isinstance(block, GalleryBlock):
        return replace(block, cards=tuple(_card(card, wrap) for card in block.cards))
    return block


def _rewrite_href(match: re.Match[str], wrap: Any) -> str:
    """Replace one anchor's ``href`` with its ``/c/`` stand-in.

    The stored value is HTML-escaped — a query string arrives as ``a=1&amp;b=2``
    — so it is unescaped before being signed, or the redirect would carry a
    literal ``&amp;`` to the destination. Nothing needs escaping on the way back
    in: a signed token is base64url plus ``:``, and none of that is markup.

    A target the wrapper declines (a ``mailto:``, an anchor, something too long)
    is put back **exactly as it was found**, escaping included, rather than as
    the unescaped string it was checked as.
    """
    prefix, href, suffix = match.group(1), match.group(2), match.group(3)
    target = unescape(href)
    wrapped = wrap(target)
    if wrapped == target:
        return match.group(0)
    return f"{prefix}{wrapped}{suffix}"


def _email_body(
    outbound: OutboundMessage,
    *,
    workspace_id: Any,
    wrap: Any,
    idempotency_key: str,
) -> OutboundMessage:
    """Rewrite anchors and append the pixel, for a workspace that asked for both.

    One query per email send, and deliberately not cached. The alternatives both
    cost more than they save: the configured cache is Django's database backend
    (SPEC §22 — no Redis), so a cache read is the same round trip this already
    makes, and a per-process memo would leave a worker mailing pixels for
    minutes after an admin switched them off, which is the one direction a
    privacy toggle must not fail in. The lookup is a single row on a unique
    index, on a path whose next step is an SMTP or provider round trip several
    orders of magnitude slower.
    """
    from apps.analytics.models import TrackingSettings

    row = TrackingSettings.objects.filter(workspace_id=workspace_id).values("wrap_email_links", "open_pixel").first()
    if row is None:
        # No row means no opt-in: both toggles default to off. See the model.
        return outbound

    html = outbound.html_body
    if row["wrap_email_links"]:
        rewritten = _ANCHOR_HREF_RE.sub(lambda match: _rewrite_href(match, wrap), html)
        # All of it or none of it. ``email_html.sanitize`` parses only the first
        # MAX_HTML_CHARS and keeps what it parsed, so a rewrite that pushes an
        # already-long body over the limit does not fail loudly — it silently
        # drops the tail, which is content the author wrote and expects to send.
        # Half a rewritten body is worse than an untracked one.
        html = rewritten if len(rewritten) <= MAX_HTML_CHARS else html

    if row["open_pixel"] and idempotency_key:
        pixel = _PIXEL_HTML.format(url=open_url(workspace_id=workspace_id, idempotency_key=idempotency_key))
        # Appended last and only if it fits. Over the limit the sanitiser would
        # cut the pixel off anyway, so adding it would cost the tail of the
        # message to buy a tag that never survives.
        if len(html) + len(pixel) <= MAX_HTML_CHARS:
            html += pixel
        else:
            logger.info("An email body is too long to carry an open pixel; sending it without one.")

    if html == outbound.html_body:
        return outbound
    return replace(outbound, html_body=html)
