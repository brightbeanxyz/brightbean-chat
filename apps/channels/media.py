"""Turning an inbound ``media_id`` into bytes a browser can show.

Two adapters store an identifier in ``EventPayload.media_ids`` rather than a URL
in ``EventPayload.attachments``, and both for the same reason: the address the
platform gave us needs *our* credentials to fetch. Telegram hands out a
``file_id`` that only becomes a URL after a ``getFile`` call, and the URL it
becomes expires in an hour. Twilio hands out a ``MediaUrl`` addressing a REST
resource under the account, which answers 401 to anyone without the Account SID
and Auth Token — or, on an account without authenticated media, answers to
anyone at all, which is a contact's picture messages behind a link we would be
handing out.

Neither is an attachment, and neither is something the inbox can put in a
``src``. This module is the one path from an identifier to displayable bytes,
and it has three parts:

:meth:`apps.channels.providers.base.Adapter.media_source`
    The platform-specific half, because only the adapter knows whether
    resolution means a ``getFile`` call, a Basic-auth GET, or something else. It
    returns *where to fetch and with what headers*, not the bytes: the fetch
    itself is the same on every platform and belongs here.

:func:`fetch_media`
    The shared half. One guarded request, one sniff, one decision about how the
    result may be served.

:func:`media_response`
    The response, built to SECURITY-BASELINE §9.

**Why the fetch goes through the SSRF guard.** SECURITY-BASELINE §6 puts
"media fetch-by-URL" on ``apps.common.outbound.guarded_request`` by name, and
there is a real question to answer here because the sibling helper,
``apps.channels.providers.base.request_json``, exists precisely for URLs an
adapter builds from constants and stored ids. The distinction is where the
*variable part* of the URL came from. A Twilio ``MediaUrl`` arrives inside a
webhook body, so it is contact-supplied and the guard is not optional. A
Telegram file URL is assembled by us, but its path comes from a ``getFile``
response, which is still somebody else's string. §6 ends with "a call site that
cannot tell which of the two it is wants the guard", so every download here
takes the guard — one call site, one rule, and ``tests/ssrf.py``'s
``guard_required()`` can be wrapped around the whole of :func:`fetch_media`.

The platform *API* call an adapter makes to resolve an id — Telegram's
``getFile`` — is the other case and keeps using ``request_json``: fixed host,
built from constants and a stored token.

**What is served back.** SECURITY-BASELINE §9's rules are not about the media
library specifically, they are about anything this deployment serves from its
own origin, and inbound media is the more hostile of the two sources: an upload
came from a team member, this came from a stranger. So the content type is
**sniffed** from the bytes with :mod:`apps.media_library.mimes` — the same
allowlist, deliberately not a second one — and anything that is not a safe
inline image is served ``Content-Disposition: attachment`` with ``nosniff``.
Bytes the sniffer does not recognise are served as ``application/octet-stream``
rather than rejected: a thread that hides an attachment it cannot name is worse
for the reader than one that offers a download, and an attachment disposition
already makes it inert.

**Why there is no signed token.** The two obvious precedents are
``apps.common.signing`` and ``apps.media_library.delivery``, and both were read
before this module was written. ``delivery`` mints a signed, unguessable,
never-expiring token because — in its own words — "a platform fetching an image
has no session and no workspace", so the token has to *be* the credential.
The consumer here is the opposite: a team member's browser, on our own origin,
holding a session and a workspace. So the route
(``inbox:media``) is an ordinary authenticated, workspace-scoped view, and
SECURITY-BASELINE §4 — which governs *unauthenticated* token routes — does not
apply to it. Two properties a token would have bought are bought better without
one:

*unguessability*
    Replaced by the membership check. A signed URL that anyone holding it can
    replay is a weaker control than one that 404s for everybody outside the
    workspace, and a URL leaks (history, referrers, a screenshot) far more
    easily than a session does.

*provenance*
    The media id is read out of the stored ``Message.body`` by row id and block
    index, never taken from the request. That is what stops this module from
    becoming an oracle that fetches *arbitrary* ids with a connection's
    credentials — a stronger guarantee than "we signed this once", and it comes
    from the query rather than from a secret.

**Cost, and the two things that bound it.** Nothing is cached server-side, so a
fetch that happens is a live call to the platform — on Telegram, two of them.
That matters more than it looks: this deployment ships four request slots (see
the time budget below), so every concurrent fetch is a quarter of the web tier
held for the duration.

The route is therefore *conditional*. ``apps.inbox.views.media`` tags each
response with an ETag over the row and the block position, and answers a
revalidation with 304 **before** calling anything here. The content at a given
``(message, block index)`` is immutable — an inbound row is never rewritten — so
that tag is stable and the 304 is always correct. Combined with
``Cache-Control: private, max-age=3600`` and a URL that does not change between
renders, a reader who scrolls back through a thread pays for each attachment
once rather than once per render.

A stored copy — resolve once into the media library — is still the better
answer for a busy deployment, and is deliberately not this change.
"""

import io
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from django.conf import settings
from django.http import HttpResponse

from apps.channels.registry import AdapterNotRegisteredError, adapter_for
from apps.common.disposition import content_disposition
from apps.common.outbound import OutboundError, guarded_request
from apps.media_library.mimes import INLINE_SAFE_MIMES, UnsupportedMediaError, extension_for, sniff

if TYPE_CHECKING:
    from apps.channels.models import ChannelConnection

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_MAX_MEDIA_BYTES",
    "MEDIA_CACHE_CONTROL",
    "MEDIA_DOWNLOAD_TIMEOUT",
    "MEDIA_RESOLVE_TIMEOUT",
    "MediaSource",
    "MediaUnavailableError",
    "ResolvedMedia",
    "fetch_media",
    "max_media_bytes",
    "media_response",
]

# ---------------------------------------------------------------------------
# The time budget
# ---------------------------------------------------------------------------
#
# **This runs on a web worker, and that is the whole constraint.** The Procfile
# and the Dockerfile both start ``gunicorn --workers 2 --threads 2`` with no
# ``--timeout``, so the deployment has *four* concurrent request slots and
# gunicorn's default 30-second worker timeout. A request that outlives that
# budget is not slow, it is a SIGKILL that takes every other request sharing the
# worker down with it.
#
# So the two halves of a resolution are budgeted together and the sum is stated
# here rather than left for someone to add up:
#
#     resolve (adapter, e.g. getFile)   5 s
#     download (guarded_request)       10 s
#     ---------------------------------------
#     worst case                       15 s   — half of gunicorn's 30 s
#
# An earlier version let the adapter use ``BACKGROUND_TIMEOUT`` (30 s) for the
# resolve step, on the reasoning that "nothing is waiting but an ``<img>``".
# That was wrong twice over: a Django worker is waiting, and 30 + 20 exceeded
# the worker timeout outright.

#: Wall clock for the adapter's own resolution call — Telegram's ``getFile``,
#: or whatever a platform needs to turn an identifier into an address. Adapters
#: import this rather than choosing their own, so the arithmetic above stays
#: true when a second platform lands.
MEDIA_RESOLVE_TIMEOUT = 5.0

#: Wall clock for the download itself, enforced by the guard as a deadline
#: across every redirect hop and every chunk of the body.
MEDIA_DOWNLOAD_TIMEOUT = 10.0

#: Default ceiling on a single attachment, when the deployment sets none.
#: Generous next to the guard's own 1 MB because that default is sized for JSON
#: API responses and this is a photograph, and bounded because the whole body is
#: buffered: the guard's cap is an *allocation* bound, so this number times the
#: number of concurrent readers is memory this process will actually hold.
DEFAULT_MAX_MEDIA_BYTES = 16 * 1024 * 1024

#: What a sniff that recognises nothing becomes. Paired with ``attachment`` and
#: ``nosniff``, which is what makes serving unknown bytes safe at all.
UNKNOWN_MIME = "application/octet-stream"

#: How a resolved attachment may be cached. ``private`` because it is one
#: workspace's contact's file and a shared cache in front of the deployment must
#: never hold it. Named rather than inlined because the route sets it on a 304
#: as well, and the two must agree — a revalidation answering with a different
#: policy than the response it is revalidating is how an entry gets dropped.
MEDIA_CACHE_CONTROL = "private, max-age=3600"


def max_media_bytes() -> int:
    """The per-attachment cap, from settings.

    Its **own** setting rather than a hard-coded argument to the guard. Passing
    ``max_bytes=`` explicitly overrides :func:`apps.common.outbound.max_response_bytes`,
    so an operator who lowers ``EXTERNAL_REQUEST_MAX_RESPONSE_BYTES`` to harden
    a deployment would silently get no protection here — a cap they cannot see
    and cannot change. ``INBOUND_MEDIA_MAX_BYTES`` is that knob, and it is
    separate from the External Request node's because the two limits answer
    genuinely different questions.

    A value that will not parse falls back to the default rather than raising:
    this is called on a request path whose contract is a 404, not a 500.
    """
    value = getattr(settings, "INBOUND_MEDIA_MAX_BYTES", DEFAULT_MAX_MEDIA_BYTES)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        logger.warning("INBOUND_MEDIA_MAX_BYTES is not a number; using %s.", DEFAULT_MAX_MEDIA_BYTES)
        return DEFAULT_MAX_MEDIA_BYTES
    return parsed if parsed > 0 else DEFAULT_MAX_MEDIA_BYTES


class MediaUnavailableError(Exception):
    """This media cannot be shown, for a reason the reader may be told.

    The message is **copy**, written here — never a platform's error text and
    never the URL. Provider prose quotes the request that produced it, access
    token included (SECURITY-BASELINE §5), and this string reaches the inbox.
    """


@dataclass(frozen=True)
class MediaSource:
    """Where an identifier resolves to, and what it takes to fetch it.

    Returned by an adapter, consumed only by :func:`fetch_media`. ``headers``
    is where credentials go — Twilio's Basic auth, a bearer token — rather than
    userinfo in the URL, which the guard refuses outright. The guard also strips
    ``Authorization`` when a redirect crosses an origin, so a platform that
    redirects its media to a CDN cannot walk our credentials off-site.

    ``headers`` is a tuple of pairs, not a dict, so the ``frozen=True`` above is
    true rather than decorative: a frozen dataclass generates ``__hash__`` from
    its fields, and one holding a dict is a class that claims to be hashable and
    raises ``TypeError`` the first time anything actually hashes it.
    ``guarded_request`` takes an iterable of pairs directly, so nothing converts.
    """

    url: str
    headers: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ResolvedMedia:
    """Bytes, plus everything the response needs to serve them safely."""

    content: bytes
    #: The **sniffed** type, never anything the platform declared.
    mime: str
    #: True only for the image types SECURITY-BASELINE §9 lets render inline.
    inline: bool
    #: Server-chosen, from the sniffed type. Never derived from the media id or
    #: from any platform-supplied name: this string reaches a response header.
    filename: str


def fetch_media(connection: "ChannelConnection", media_id: str) -> ResolvedMedia:
    """Resolve ``media_id`` on ``connection`` and return displayable bytes.

    Raises :class:`MediaUnavailableError` for **every** failure — no adapter, no
    credentials, a platform that said no, a body over :func:`max_media_bytes`, a
    URL the SSRF guard refused, and anything an adapter raises on its way to
    either. The caller's job is to turn that into a 404 and a tombstone, so the
    distinctions are deliberately not exposed.
    """
    if not isinstance(media_id, str) or not media_id.strip():
        raise MediaUnavailableError("This attachment was recorded without an identifier.")

    try:
        adapter = adapter_for(connection.platform)
    except AdapterNotRegisteredError as exc:
        raise MediaUnavailableError("This channel cannot resolve attachments.") from exc

    source = _resolve(adapter, connection, media_id)
    response = _download(connection, source)

    if not response.ok:
        logger.info(
            "Media on connection %s: %s answered HTTP %s.", connection.pk, response.final_host, response.status_code
        )
        raise MediaUnavailableError("The platform would not serve this attachment.")
    if response.truncated:
        raise MediaUnavailableError("This attachment is too large to show here.")
    if not response.content:
        raise MediaUnavailableError("The platform served an empty attachment.")

    return _describe(response.content)


def _resolve(adapter: Any, connection: "ChannelConnection", media_id: str) -> MediaSource:
    """Ask the adapter where to fetch, and hold it to its contract.

    Two containments, because an adapter can break its promise in two different
    ways and only one of them is an exception.

    **Raising.** ``Adapter.media_source`` documents "return None rather than
    raising", but a docstring is not a guarantee: an implementation reaches
    encrypted credentials, a platform SDK and an HTTP client, any of which can
    raise something this module has never heard of.

    **Returning nonsense.** A dict, a bare string, a ``MediaSource`` whose
    ``headers`` is the plain mapping an older draft of the docs showed — none of
    these raise *here*, they raise several lines later inside the guard, on an
    ``AttributeError`` or a ``TypeError`` that no caller is catching. That is the
    same 500 by a longer route, and it is the more likely of the two: dataclasses
    do not enforce their annotations, so ``MediaSource(url=..., headers={...})``
    is constructed happily and only fails when something tries to use it.

    Either way the endpoint's contract is a bare 404, so both end here.
    ``BaseException`` is deliberately not caught: a worker being shut down
    should shut down.
    """
    try:
        source = adapter.media_source(connection, media_id)
    except Exception as exc:
        # The exception's *type* and nothing else, following the rule
        # ``request_json`` states at its own site: a transport error's text
        # carries the full URL, query string and any token in it, and this line
        # goes to the same logs as everything else (SECURITY-BASELINE §5).
        logger.error(
            "Adapter for %s raised %s while resolving media on connection %s.",
            connection.platform,
            type(exc).__name__,
            connection.pk,
        )
        raise MediaUnavailableError("This attachment could not be resolved.") from exc

    if source is None:
        raise MediaUnavailableError("This attachment is no longer available from the platform.")
    return _validated(source, connection)


def _validated(source: Any, connection: "ChannelConnection") -> MediaSource:
    """``source`` as something this module is willing to hand the guard.

    Rebuilt rather than merely checked, so what reaches ``guarded_request`` is a
    URL that is a string and headers that are pairs of strings, whatever the
    adapter actually put in the fields. A mapping is accepted and normalised —
    it is the shape an adapter author would most naturally reach for, the guard
    itself takes either, and refusing it would turn a stylistic mismatch into a
    missing picture.

    Every rejection is logged at ``error``: unlike a platform saying no, this is
    a bug in this deployment's own code, and the reader's 404 is the only other
    trace it would leave.
    """
    if not isinstance(source, MediaSource):
        logger.error(
            "Adapter for %s returned %s rather than a MediaSource.", connection.platform, type(source).__name__
        )
        raise MediaUnavailableError("This attachment could not be resolved.")

    if not isinstance(source.url, str) or not source.url.strip():
        logger.error("Adapter for %s returned a MediaSource with no usable url.", connection.platform)
        raise MediaUnavailableError("This attachment could not be resolved.")

    raw = source.headers
    try:
        pairs = raw.items() if isinstance(raw, Mapping) else raw
        headers = tuple((str(name), str(value)) for name, value in pairs)
    except (TypeError, ValueError, AttributeError) as exc:
        # TypeError: not iterable at all. ValueError: iterable, but not of
        # pairs. Both would otherwise surface from inside ``_clean_headers``,
        # where nothing is catching them.
        logger.error(
            "Adapter for %s returned MediaSource headers that are not name/value pairs (%s).",
            connection.platform,
            type(exc).__name__,
        )
        raise MediaUnavailableError("This attachment could not be resolved.") from exc

    return MediaSource(url=source.url, headers=headers)


def _download(connection: "ChannelConnection", source: MediaSource) -> Any:
    """One guarded GET, with every failure contained.

    The broad ``except`` is the backstop for :func:`fetch_media`'s stated
    contract — "raises MediaUnavailableError for every failure" — and not a
    substitute for :func:`_validated`, which is what stops the predictable
    malformations from reaching here at all. It stays because a 500 from this
    endpoint is a worse outcome than a swallowed surprise, and the surprise is
    still logged with its type.
    """
    try:
        return guarded_request(
            "GET",
            source.url,
            headers=source.headers,
            timeout=MEDIA_DOWNLOAD_TIMEOUT,
            max_bytes=max_media_bytes(),
        )
    except OutboundError as exc:
        # ``exc`` names the host and nothing else — the guard is careful about
        # that — but the *URL* would carry a bot token in its path, so neither
        # it nor ``source.url`` appears here.
        logger.info("Media on connection %s could not be fetched: %s", connection.pk, exc)
        raise MediaUnavailableError("This attachment could not be fetched.") from exc
    except Exception as exc:
        logger.error("Fetching media on connection %s raised an unexpected %s.", connection.pk, type(exc).__name__)
        raise MediaUnavailableError("This attachment could not be fetched.") from exc


def _describe(content: bytes) -> ResolvedMedia:
    """Decide what ``content`` is and how it may be served (§9).

    Sniffing, never a declared header: the ``Content-Type`` on the response
    comes from the same stranger as the bytes, and the attack it enables —
    markup served inline from our own origin — is exactly the stored XSS
    :mod:`apps.media_library.mimes` was written to stop. Reusing that module
    rather than writing a second sniffer is the point; a type it rejects is a
    type this deployment has already decided not to render.
    """
    try:
        mime = sniff(io.BytesIO(content))
    except UnsupportedMediaError:
        # Not an error here, unlike on the upload path. An upload can be refused
        # and the person told why; a contact has already sent this, and the
        # reader's alternative to a download link is a thread with a hole in it.
        return ResolvedMedia(content=content, mime=UNKNOWN_MIME, inline=False, filename="attachment")

    inline = mime in INLINE_SAFE_MIMES
    return ResolvedMedia(content=content, mime=mime, inline=inline, filename=f"attachment.{extension_for(mime)}")


def media_response(resolved: ResolvedMedia) -> HttpResponse:
    """Serve ``resolved`` under SECURITY-BASELINE §9's rules.

    ``Content-Disposition`` is built by
    :func:`apps.common.disposition.content_disposition` — the same function the
    asset delivery view uses, because a header a filename can break out of is a
    header a filename can break out of on either route.
    """
    response = HttpResponse(resolved.content, content_type=resolved.mime)
    response["Content-Disposition"] = content_disposition(inline=resolved.inline, filename=resolved.filename)
    response["X-Content-Type-Options"] = "nosniff"
    # The route overwrites this with the identical value on its way out, and
    # sets it on a 304 too; it is set here so a caller that skips the view still
    # gets a response that is safe to cache.
    response["Cache-Control"] = MEDIA_CACHE_CONTROL
    return response
