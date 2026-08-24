"""The two public token routes: ``/c/`` click redirect and ``/o/`` open pixel.

Both are unauthenticated by design (SECURITY-BASELINE §4). The caller is a
messaging platform, a browser following a button, or a mail client fetching an
image — none of them has a session, and the signed token is the whole credential.
``apps/common/signing.py`` reserves both by name for this issue and
``tests/idor.py`` carries the waiver that states the position.

--------------------------------------------------------------------------
``/c/`` is a redirect, so it is an open redirect unless it refuses to be
--------------------------------------------------------------------------

Three rules, in this order:

1. **The destination is read from the verified payload.** Nothing off the query
   string can change it. A ``?url=`` on the request is data, not a parameter.
2. **The scheme is re-checked here**, not only where the token was minted, with
   ``apps.common.validators.is_renderable_url`` — http/https with a host, and
   nothing else. A token minted by an older release, or one whose wrapper is
   later changed, still cannot point at ``javascript:`` or ``file:``.
3. **A refused destination is a 404**, the same bare one a bad signature gets.

The incoming query string is appended to the destination, because a link that
loses its own tracking parameters or its ``?page=2`` on the way through is a
broken link — and a fragment is never sent by a browser, so there is none to
carry.

--------------------------------------------------------------------------
Counting is best-effort; redirecting is not
--------------------------------------------------------------------------

The redirect happens even when the counter cannot be written: the flow may have
been deleted since the message went out, and answering 404 to somebody who
pressed a button in a real message because a row is gone would break the link to
punish the reader for a workspace's housekeeping.

**There is deliberately no per-address throttle on the counter**, and the reason
is worth stating because an earlier version of this module had one. Client
addresses are not distinguishable here: ``apps.common.net.get_client_ip``
ignores ``X-Forwarded-For`` unless the peer is in ``TRUSTED_PROXIES``, and SPEC
§20's reference deployment puts Caddy in front of gunicorn — so with that
unset, *every* click reports the proxy's address and one limit governs the whole
deployment. A corporate NAT or a mail-scanner egress does the same thing to a
correctly configured one. The result was legitimate clicks silently dropped from
a campaign's numbers, which is worse than the inflation it was guarding against:
a click counter is inherently replayable by anyone holding the link, SPEC §18
asks for no defence against that, and neither ``/u/`` nor ``/m/`` — the other
public token routes — throttles either.

What the endpoint does instead is stay cheap: one indexed lookup and one upsert,
both skipped entirely when the token names no live flow.

--------------------------------------------------------------------------
``/o/`` bumps a status; it does not invent a counter
--------------------------------------------------------------------------

SPEC §5 gives ``node_stat_daily`` four columns and "opened" is not one of them,
so the pixel writes no counter. What it does is SPEC §6.7's "per-message
granularity not required in v1 beyond ``message.status``": it advances the
message along the delivery ladder to ``read``, through
:func:`apps.messaging.ingest.apply_receipt`, which is the one implementation of
that ladder and the one place the compare-and-set lives.

The ladder is also why an open moves the ``delivered`` counter even though it
adds no counter of its own: the message crossed that rung on its way to ``read``,
and a mail client displaying a message is proof it arrived. For email that is
often the only such proof — SMTP has no delivery receipt — so a pixel that
refused to record it would leave a whole channel's ``delivered`` column at zero.

The response is a 1×1 GIF with ``Cache-Control: no-store`` — a cached pixel is an
open that is never reported again — and it is returned whether or not anything
was found, so a fetch tells the fetcher nothing about which messages exist.
"""

import base64
import logging
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from django.http import Http404, HttpRequest, HttpResponse, HttpResponseRedirect
from django.views.decorators.http import require_GET

from apps.analytics import counters, tracking
from apps.common.validators import is_renderable_url

logger = logging.getLogger(__name__)

__all__ = ["click_redirect", "open_pixel"]

#: The smallest transparent GIF there is. Inline rather than a static file: it
#: has to be served with ``no-store`` and no redirect, and a 43-byte constant is
#: cheaper than a staticfiles lookup on every open.
_PIXEL_GIF = base64.b64decode(b"R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")

#: A token longer than this is refused before any signature work. Django's own
#: URL length limits are generous and the signer would happily spend time on a
#: megabyte of base64 (SECURITY-BASELINE §7).
MAX_TOKEN_CHARS = 4096


@require_GET
def click_redirect(request: HttpRequest, token: str) -> HttpResponse:
    """Count the click, then 302 to the destination inside the token."""
    if len(token) > MAX_TOKEN_CHARS:
        raise Http404
    target = tracking.click_target_from_token(token)

    destination = target.url
    if not is_renderable_url(destination):
        # A token whose payload is not an http(s) URL. Only reachable with our
        # own signing key, so this is a bug or an old format rather than an
        # attack — and the answer is the same bare 404 either way.
        raise Http404

    _count(target)
    return HttpResponseRedirect(_with_query(destination, request.META.get("QUERY_STRING", "")))


@require_GET
def open_pixel(request: HttpRequest, token: str) -> HttpResponse:
    """Mark the message read, then answer a 1×1 GIF whatever happened."""
    if len(token) > MAX_TOKEN_CHARS:
        raise Http404
    target = tracking.open_target_from_token(token)
    _mark_read(target)

    response = HttpResponse(_PIXEL_GIF, content_type="image/gif")
    response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response["Content-Length"] = str(len(_PIXEL_GIF))
    return response


def _count(target: tracking.ClickTarget) -> None:
    """Add one to the node's ``clicked`` counter.

    Resolves the flow to find its workspace rather than trusting a workspace id
    in the payload: the row is the authority on which tenant this counter belongs
    to, and a deleted flow means there is nothing to count — not a broken link.
    """
    from apps.flows.models import Flow

    workspace_id = _flow_workspace(Flow, target.flow_id)
    if workspace_id is None:
        return
    counters.record_click(workspace_id=workspace_id, flow_id=target.flow_id, node_id=target.node_id)


def _flow_workspace(model: Any, flow_id: str) -> Any:
    """The workspace owning ``flow_id``, or ``None`` if it is gone.

    A deliberate, greppable ``.unscoped()``: there is no session and therefore no
    workspace on this path, and the signed token is what authorises the read —
    the same position ``apps.channels.views_unsubscribe._identity`` takes
    (CONTRIBUTING). It is the only unscoped query in this module.
    """
    from django.core.exceptions import ValidationError

    try:
        row = model.objects.unscoped().filter(pk=flow_id).values("workspace_id").first()
    except (ValidationError, ValueError, TypeError):
        return None
    return None if row is None else row["workspace_id"]


def _mark_read(target: tracking.OpenTarget) -> None:
    """Advance the message this token names to ``read``, if it is still behind it.

    Everything about *whether* it may move is :func:`apps.messaging.ingest.apply_receipt`'s
    — a message that already reported ``read`` does not move twice, a deleted one
    never moves at all, and the compare-and-set is what makes two mail clients
    fetching the same pixel a single transition.
    """
    from django.core.exceptions import ValidationError

    from apps.messaging.ingest import apply_receipt
    from apps.messaging.models import Message, MessageDirection, MessageStatus

    if not target.workspace_id or not target.idempotency_key:
        return
    try:
        message = (
            Message.objects.for_workspace(target.workspace_id)
            .filter(idempotency_key=target.idempotency_key, direction=MessageDirection.OUT)
            .order_by("created_at")
            .first()
        )
    except (ValidationError, ValueError, TypeError):
        return
    if message is None:
        return
    apply_receipt(message, MessageStatus.READ.value)


def _with_query(destination: str, query: str) -> str:
    """Carry the request's own query string through to the destination.

    Merged by concatenation rather than by parsing and re-encoding: the
    destination's parameters are the author's and the request's are whatever the
    platform appended, and re-encoding either would change bytes a receiving
    server may be signing over.
    """
    if not query:
        return destination
    parts = urlsplit(destination)
    merged = f"{parts.query}&{query}" if parts.query else query
    return urlunsplit((parts.scheme, parts.netloc, parts.path, merged, parts.fragment))
