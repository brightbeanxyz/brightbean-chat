"""Public delivery URLs — what an adapter hands to a platform.

A platform fetching an image has no session and no workspace. It has a URL, and
that URL has to be unguessable, long-lived, and safe to serve to whatever
fetches it. So the token *is* the credential, minted with
:mod:`apps.common.signing` under its own purpose salt — the same discipline the
unsubscribe, click-tracking and open-pixel routes use, and for the same reason:
one token format means one place to audit and one place to rotate.

Three properties worth stating explicitly, because a reviewer will look for each:

``purpose="media-delivery"``
    A token minted here cannot be replayed against ``/internal/tick`` or an
    unsubscribe route, even though every one of them is signed with the same
    ``SECRET_KEY``.

``max_age=None``
    Deliberate. A platform may fetch the URL minutes after the send, a broadcast
    may sit in a queue, and an email body may be opened next week. Expiry is not
    the control here; unguessability is, and revocation is deleting the asset —
    the row is what this view reads, so a deleted asset 404s every URL ever
    minted for it. That is the "deleting an asset invalidates resolution"
    behaviour issue #16 asks for.

``accept_versions``
    A set, so changing the payload shape later is a rollout rather than a
    cutover that 404s every URL already sitting in a platform's cache.

The response itself follows SECURITY-BASELINE §9: the ``Content-Type`` is the
mime this deployment sniffed from the bytes, never a client's header or a
filename guess, and only the image/audio/video kinds are served ``inline``.
Everything else is ``attachment``, which is what makes a document harmless in a
browser even before ``nosniff`` is considered.

**One known asymmetry between the two storage backends.** On local disk the
response carries ``X-Content-Type-Options: nosniff``. On S3 it cannot: a
presigned GET can override ``Content-Type`` and ``Content-Disposition`` (both of
which this module pins) but S3 has no way to return ``X-Content-Type-Options``.
What closes that gap is upstream — the allowlist in
:mod:`apps.media_library.mimes` never stores SVG, HTML or an unrecognised
signature, so there is no markup in the bucket for a browser to sniff its way
into, and the pinned ``Content-Type`` removes the ambiguity sniffing exists to
resolve. It is a residual, and it is written down here rather than glossed.
"""

from typing import Any
from urllib.parse import urljoin

from django.conf import settings
from django.http import FileResponse, HttpResponseRedirect
from django.http.response import HttpResponseBase
from django.urls import reverse

from apps.common.signing import sign, unsign_or_404
from apps.media_library import storage
from apps.media_library.mimes import INLINE_SAFE_MIMES

__all__ = ["ASSET_KEY", "PURPOSE", "THUMBNAIL_KEY", "delivery_response", "delivery_url", "read_token"]

PURPOSE = "media-delivery"
ASSET_KEY = "a"
THUMBNAIL_KEY = "t"

#: Versions of the token payload this deployment still honours. Add to the set
#: when the shape changes; remove the old entry only once those URLs are gone.
ACCEPTED_VERSIONS = (1,)


def delivery_token(asset: Any, *, thumbnail: bool = False) -> str:
    payload: dict[str, Any] = {ASSET_KEY: str(asset.pk)}
    if thumbnail:
        payload[THUMBNAIL_KEY] = True
    return sign(payload, purpose=PURPOSE)


def delivery_url(asset: Any, *, thumbnail: bool = False, absolute: bool = True) -> str:
    """The URL an adapter sends to a platform.

    Absolute by default: the consumer is an external fetcher, not a browser
    holding an origin, so a path alone would be useless. ``APP_URL`` is the
    deployment's own configured address rather than anything read off the
    request, because the send path runs in a worker where there is no request.
    """
    path = reverse("media_delivery", kwargs={"token": delivery_token(asset, thumbnail=thumbnail)})
    if not absolute:
        return path
    return urljoin(settings.APP_URL.rstrip("/") + "/", path.lstrip("/"))


def read_token(token: str) -> tuple[str, bool]:
    """``(asset id, is thumbnail)``, or ``Http404`` for any failure at all.

    Bad signature, wrong purpose, unknown version and malformed payload are
    deliberately indistinguishable — ``unsign_or_404`` makes every rejection the
    same bare 404, so a caller learns nothing from one.
    """
    payload = unsign_or_404(token, purpose=PURPOSE, max_age=None, accept_versions=ACCEPTED_VERSIONS)
    asset_id = payload.get(ASSET_KEY)
    if not isinstance(asset_id, str):
        from django.http import Http404

        raise Http404
    return asset_id, bool(payload.get(THUMBNAIL_KEY))


def delivery_response(asset: Any, *, thumbnail: bool = False) -> HttpResponseBase:
    """Serve the asset's bytes, or redirect to storage when that is safe.

    Thumbnails are always JPEG images this app generated itself, so they are
    inline regardless of the asset's own kind.
    """
    if thumbnail:
        field, mime, inline = asset.thumbnail, "image/jpeg", True
        filename = f"{asset.pk}.jpg"
    else:
        field, mime = asset.file, asset.mime
        inline = mime in INLINE_SAFE_MIMES
        filename = asset.filename

    if not field:
        from django.http import Http404

        raise Http404

    disposition = storage.content_disposition(inline=inline, filename=filename)

    # Called through the module rather than imported by name: the three storage
    # functions are this app's only knowledge that S3 exists, and going through
    # the module keeps them a single monkeypatch point for the tests that
    # exercise the S3 path without a bucket.
    if storage.can_presign():
        return HttpResponseRedirect(storage.presigned_get_url(field.name, mime=mime, disposition=disposition))

    # Local disk, or an S3 deployment whose custom domain has disabled signing
    # (common.W001) — proxying is both correct and the only option that works.
    response = FileResponse(field.open("rb"), content_type=mime)
    response["Content-Disposition"] = disposition
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, max-age=3600"
    return response
