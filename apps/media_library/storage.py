"""Where bytes live, and how a delivery URL for them is built.

Two jobs, both of which exist to keep client-supplied strings away from
anything that matters:

**Keys are chosen by the server.** ``upload_to`` never sees the uploaded
filename. The path is built from the workspace id, the asset's own UUIDv7 and
the extension of the *sniffed* mime, so a name like ``../../etc/passwd`` or
``payload.html`` cannot traverse, collide, or steer the ``Content-Type``
django-storages infers from a name. Studio's ``generate_storage_key`` had the
same instinct but copied the extension from the client's declared filename;
here it comes from :mod:`apps.media_library.mimes`, which read the bytes.

**boto3 stays behind a seam.** :func:`is_s3_backend`, :func:`can_presign` and
:func:`presigned_get_url` are the only places this app knows S3 exists, which is
what lets the test suite exercise the S3 delivery path with three monkeypatches
and no bucket. The import of boto3 is deferred inside them so a local-disk
deployment never loads it.
"""

from typing import Any
from urllib.parse import quote

from django.conf import settings
from django.core.files.storage import default_storage

from apps.media_library.mimes import ALLOWED_MIMES

__all__ = [
    "asset_upload_to",
    "can_presign",
    "content_disposition",
    "is_s3_backend",
    "presigned_get_url",
    "thumbnail_upload_to",
]

#: How long a minted storage URL stays valid. Deliberately short: the delivery
#: view mints one per request, and the long-lived thing is our own signed token,
#: not the storage URL it redirects to.
PRESIGNED_URL_TTL = 3600


def asset_upload_to(instance: Any, filename: str) -> str:
    """``media/<workspace>/<asset id>.<ext>`` — ``filename`` is ignored.

    The parameter exists because Django's ``upload_to`` protocol passes it. It
    is unused on purpose, and that is the point worth reading twice: the only
    path components are values this server chose.
    """
    extension = ALLOWED_MIMES.get(instance.mime, (None, "bin"))[1]
    return f"media/{instance.workspace_id}/{instance.pk}.{extension}"


def thumbnail_upload_to(instance: Any, filename: str) -> str:
    return f"media/{instance.workspace_id}/thumbs/{instance.pk}.jpg"


def is_s3_backend() -> bool:
    """True when ``default_storage`` is the S3/R2 backend.

    Detected by module path rather than ``isinstance`` so this module never
    imports the S3 backend — and transitively boto3 — on a local-disk
    deployment. Studio's check, kept verbatim in spirit.
    """
    return type(default_storage).__module__.startswith("storages.backends.s3")


def can_presign() -> bool:
    """Whether a presigned GET is actually obtainable.

    ``AWS_S3_CUSTOM_DOMAIN`` routes URL generation through the custom domain,
    where django-storages signs nothing unless both CloudFront signer settings
    are present — the exact misconfiguration ``apps.common.checks`` raises
    ``common.W001`` for. On such a deployment a redirect would hand out a URL
    that 403s, so the delivery view falls back to proxying instead. A warning at
    deploy time and a working response at request time, rather than one or the
    other.
    """
    if not is_s3_backend():
        return False
    if getattr(settings, "AWS_S3_CUSTOM_DOMAIN", ""):
        return bool(getattr(settings, "AWS_CLOUDFRONT_KEY_ID", "")) and bool(
            getattr(settings, "AWS_CLOUDFRONT_KEY", "")
        )
    return True


def content_disposition(*, inline: bool, filename: str) -> str:
    """A ``Content-Disposition`` value that a filename cannot break out of.

    Uploaded filenames reach two header-shaped places — the local response
    header and the ``ResponseContentDisposition`` parameter of a presigned S3
    URL — and both are injection sinks for a name containing a quote or a
    newline. Control characters are dropped, the ASCII fallback is stripped to
    a conservative set, and the real name rides in the RFC 5987 ``filename*``
    form where percent-encoding makes it inert.
    """
    safe = "".join(char for char in filename if char.isprintable() and char not in '"\\')[:200]
    ascii_fallback = "".join(char if 32 <= ord(char) < 127 else "_" for char in safe) or "download"
    disposition = "inline" if inline else "attachment"
    return f"{disposition}; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quote(safe or 'download')}"


def _client_and_bucket() -> tuple[Any, str]:
    return default_storage.connection.meta.client, default_storage.bucket_name  # type: ignore[attr-defined]


def _normalize(name: str) -> str:
    """Apply the backend's LOCATION prefix and traversal guard.

    Mirrors S3Storage's own ``_normalize_name(clean_name(...))``: presign and
    the stored object have to agree on the exact key, and ``LOCATION`` is the
    thing that silently makes them disagree.
    """
    from storages.utils import clean_name

    return default_storage._normalize_name(clean_name(name))  # type: ignore[attr-defined]


def presigned_get_url(name: str, *, mime: str, disposition: str, expires_in: int = PRESIGNED_URL_TTL) -> str:
    """A presigned GET whose response headers we pin.

    ``ResponseContentType`` is the mime *we* sniffed from the bytes, not
    whatever S3 stored from the object key, and ``ResponseContentDisposition``
    carries the attachment decision. Those two overrides are the reason the S3
    delivery path is safe without ``X-Content-Type-Options`` — S3 has no way to
    return that header, and pinning the type is what removes the ambiguity it
    would otherwise resolve.
    """
    client, bucket = _client_and_bucket()
    return str(
        client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": bucket,
                "Key": _normalize(name),
                "ResponseContentType": mime,
                "ResponseContentDisposition": disposition,
            },
            ExpiresIn=int(expires_in),
        )
    )
