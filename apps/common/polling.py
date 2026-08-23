"""Conditional-GET support for HTMX polling (SPEC §14).

SPEC §14 mandates polling rather than SSE, and asks for "HTMX polling every 3 s
on list and open thread (ETag/304 to keep payloads near-zero when unchanged)".
This module is that half of it, kept out of any one app because the shape
generalises: compute a cheap version token for whatever the response depends on,
hand it here, and an unchanged poll costs a request line and a 304.

The version token is the interesting part and it belongs to the caller. The
cheap form — ``aggregate(Max("updated_at"), Count("id"))`` over the rows the
response renders — catches every edit *and* every deletion, which a bare
``Max`` misses: delete the most recently touched row and the maximum walks
*backwards* to a value the client has already seen, so a stale render would
survive the change that removed it. Anything else the markup varies on (the
filters, the viewer, the page cursor) is another part of the token, because two
requests that render differently must not share an ETag.

Weak tags (``W/"..."``): these responses are semantically, not byte-for-byte,
equivalent — a relative timestamp inside the markup can differ between two
renders of the same data — and a weak tag is exactly the promise HTTP has for
that.

The client half lives in ``templates/inbox/list.html``. Two things are worth
knowing before reusing this from another page:

* **htmx does not send ``If-None-Match``.** Something has to remember the last
  ETag per URL and set the header.
* **htmx 2.0's default ``responseHandling`` swaps on 304.** Its second rule is
  ``{code: "[23]..", swap: true}``, and ``"304"`` matches it — so out of the box
  a 304's empty body is swapped over the polled region and the pane goes blank.
  A page that polls must prepend ``{code: "304", swap: false}``.
"""

import hashlib
from collections.abc import Callable
from typing import Any

from django.http import HttpRequest, HttpResponse, HttpResponseNotModified

__all__ = ["conditional", "if_none_match", "version_etag"]


def version_etag(*parts: Any) -> str:
    """A weak ETag over ``parts``, whatever they are.

    ``None`` and a literal ``"None"`` have to hash differently — an empty
    workspace's ``Max("updated_at")`` is ``None``, and a filter value of the
    string "None" is a thing a query string can carry — so each part is joined
    with its type name rather than by ``str()`` alone.
    """
    material = "\x1f".join(f"{type(part).__name__}:{part}" for part in parts)
    digest = hashlib.blake2b(material.encode("utf-8"), digest_size=16).hexdigest()
    return f'W/"{digest}"'


def if_none_match(request: HttpRequest, etag: str) -> bool:
    """Does the client already hold ``etag``?

    RFC 9110 §13.1.2: the header is a comma-separated list, ``*`` matches
    anything the server holds, and the comparison for ``If-None-Match`` is
    **weak** — ``W/"x"`` and ``"x"`` are a match. Django's own
    ``get_conditional_response`` implements all of that, but only as part of a
    full conditional-request pipeline that also wants ``Last-Modified`` and
    rewrites the response; this is the one question the pollers ask.
    """
    header = request.headers.get("If-None-Match", "")
    if not header:
        return False
    wanted = _weak(etag)
    for candidate in header.split(","):
        candidate = candidate.strip()
        if candidate == "*":
            return True
        if candidate and _weak(candidate) == wanted:
            return True
    return False


def _weak(etag: str) -> str:
    """The opaque-string half of an entity tag, for weak comparison."""
    if etag.startswith(("W/", "w/")):
        etag = etag[2:]
    return etag.strip()


def conditional(request: HttpRequest, etag: str, build: Callable[[], HttpResponse]) -> HttpResponse:
    """304 when the client is current, otherwise ``build()`` tagged with ``etag``.

    ``build`` is a callable rather than a response so an unchanged poll never
    renders the template it would have thrown away — which is the whole point of
    the exercise at one request every three seconds per open tab.

    ``Cache-Control: no-store`` is deliberate and is not in tension with the
    ETag. The revalidation here is driven by JavaScript that remembers the last
    tag, not by the browser's HTTP cache; letting the cache in as well would
    mean a 304 on the wire is replayed to the caller as a 200 from cache, and
    the "did anything change?" answer the poller wants would be lost on the way.
    """
    if if_none_match(request, etag):
        response: HttpResponse = HttpResponseNotModified()
    else:
        response = build()
        response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "no-store"
    return response
