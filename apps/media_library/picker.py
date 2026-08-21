"""The picker JSON contract.

**This docstring is the contract.** Four later issues consume this endpoint —
the flow builder's media block (#10), the broadcast composer (#23), the inbox
attachment button (#24) and the email editor (#21) — and they code against what
is written here, not against this app's internals. Fields are added, never
renamed or removed, and a consumer that reads an unknown key ignores it.

Request
-------
``GET /w/<workspace_id>/media/picker/``

Gated on workspace membership alone, not on ``manage_media``: an Agent who may
not upload still has to be able to attach an existing asset to an inbox reply.
Cross-workspace access answers 404 like every other tenant route.

===============  ====================================================
``q``            Free-text match on filename, title and alt text.
``kind``         One of ``image`` / ``audio`` / ``video`` / ``file``.
``folder``       A folder id, or ``root`` for assets in no folder.
``platform``     A ``apps.common.platforms.Platform`` value. Populates
                 ``platform_warnings``; advisory only, never filters.
``cursor``       Opaque; pass back the previous ``next_cursor``.
``limit``        1–``MAX_LIMIT``, default ``DEFAULT_LIMIT``.
===============  ====================================================

Response
--------
::

    {
      "results": [
        {
          "id": "0199c0de-...",            # pass this to media_library.resolve
          "kind": "image",                  # image | audio | video | file
          "mime": "image/png",              # sniffed from the bytes, not declared
          "filename": "logo.png",
          "title": "",
          "alt_text": "",
          "size": 12345,                    # bytes
          "width": 800, "height": 600,      # 0 when unknown (non-images)
          "folder_id": null,
          "url": "https://host/m/<token>/", # signed, long-lived delivery URL
          "thumbnail_url": null,            # images only; null otherwise
          "created_at": "2026-08-21T10:00:00+00:00",
          "platform_warnings": []           # only populated when ?platform= is given
        }
      ],
      "folders": [{"id": "...", "name": "Brand", "parent_id": null}],
      "next_cursor": null                   # null when there are no more results
    }

Notes for consumers
-------------------
* Store ``id``. Never store ``url`` — it is minted per request, and the whole
  point of the library is that a block survives a storage backend change.
* ``platform_warnings`` is a list of strings meant to be shown next to the
  asset. It never means "cannot attach"; the target platform is not fixed until
  send time, and an asset too large for WhatsApp is fine for Telegram.
* ``next_cursor`` is opaque and short-lived by construction (it encodes the last
  row's ordering key). Do not parse it.
"""

from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import datetime
from typing import Any
from uuid import UUID

from django.db.models import Q, QuerySet

from apps.media_library.delivery import delivery_url
from apps.media_library.mimes import MediaKind
from apps.media_library.models import MediaAsset, MediaFolder
from apps.media_library.platform_limits import warnings_for

__all__ = ["DEFAULT_LIMIT", "MAX_LIMIT", "picker_payload", "serialize_asset", "serialize_folder"]

DEFAULT_LIMIT = 40
MAX_LIMIT = 100

#: Sentinel for "assets that are in no folder at all". A caller cannot express
#: that with an id, and an absent ``folder`` already means "do not filter".
ROOT_FOLDER = "root"


def serialize_folder(folder: MediaFolder) -> dict[str, Any]:
    return {"id": str(folder.pk), "name": folder.name, "parent_id": str(folder.parent_id) if folder.parent_id else None}


def serialize_asset(asset: MediaAsset, *, platform: str = "") -> dict[str, Any]:
    return {
        "id": str(asset.pk),
        "kind": asset.kind,
        "mime": asset.mime,
        "filename": asset.filename,
        "title": asset.title,
        "alt_text": asset.alt_text,
        "size": int(asset.size),
        "width": int(asset.width),
        "height": int(asset.height),
        "folder_id": str(asset.folder_id) if asset.folder_id else None,
        "url": delivery_url(asset),
        "thumbnail_url": delivery_url(asset, thumbnail=True) if asset.thumbnail else None,
        "created_at": asset.created_at.isoformat(),
        "platform_warnings": (
            warnings_for(platform=platform, kind=asset.kind, size=int(asset.size)) if platform else []
        ),
    }


def search(queryset: QuerySet, term: str) -> QuerySet:
    """Case-insensitive match across the three human-authored fields.

    ORM ``icontains`` rather than Studio's ``SearchVector``/``SearchRank``: full
    text search buys stemming and ranking that a library of a few thousand
    filenames does not need, and it needs an index and a migration to not be
    slow. Plain matching is predictable, and — the part that matters for
    SECURITY-BASELINE §7 — it compiles entirely through the ORM with no
    string-built SQL and no user-controlled field names.
    """
    return queryset.filter(Q(filename__icontains=term) | Q(title__icontains=term) | Q(alt_text__icontains=term))


def _encode_cursor(asset: MediaAsset) -> str:
    raw = f"{asset.created_at.isoformat()}|{asset.pk}".encode()
    return urlsafe_b64encode(raw).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, UUID] | None:
    """``None`` for anything malformed — a bad cursor restarts, never 500s."""
    try:
        created, _, pk = urlsafe_b64decode(cursor.encode()).decode().partition("|")
        return datetime.fromisoformat(created), UUID(pk)
    except (ValueError, TypeError, UnicodeDecodeError):
        return None


def _as_uuid(value: str) -> UUID | None:
    """A query parameter that must be an id, or ``None``.

    Django raises rather than returning nothing when a malformed UUID reaches a
    ``filter()``, so an id-shaped parameter is parsed before it gets there — a
    junk ``?folder=`` is an empty result, not a 500.
    """
    try:
        return UUID(value)
    except (ValueError, AttributeError, TypeError):
        return None


def picker_payload(
    *,
    workspace: Any,
    term: str = "",
    kind: str = "",
    folder: str = "",
    platform: str = "",
    cursor: str = "",
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Build the documented response for one picker request."""
    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))

    # Annotated as the plain QuerySet: ``search()`` and ``none()`` return one,
    # and the scope has already been applied and travels with the clone.
    assets: QuerySet = MediaAsset.objects.for_workspace(workspace).select_related("folder")

    if kind in MediaKind.values:
        assets = assets.filter(kind=kind)
    if folder == ROOT_FOLDER:
        assets = assets.filter(folder__isnull=True)
    elif folder:
        folder_pk = _as_uuid(folder)
        # An id from another workspace simply matches nothing: the queryset is
        # already scoped, so this cannot become a cross-tenant read. A junk id
        # is filtered to empty rather than passed through as ``folder_id=None``,
        # which Django would render as ``IS NULL`` and quietly return the root
        # of the library instead.
        assets = assets.none() if folder_pk is None else assets.filter(folder_id=folder_pk)
    if term:
        assets = search(assets, term)

    # Keyset pagination on (created_at, id) — the model's own ordering, and a
    # stable one, so a row inserted mid-scroll cannot shift a page boundary the
    # way OFFSET does.
    assets = assets.order_by("-created_at", "-id")
    if cursor:
        decoded = _decode_cursor(cursor)
        if decoded is not None:
            created, pk = decoded
            assets = assets.filter(Q(created_at__lt=created) | Q(created_at=created, pk__lt=pk))

    page = list(assets[: limit + 1])
    has_more = len(page) > limit
    page = page[:limit]

    return {
        "results": [serialize_asset(asset, platform=platform) for asset in page],
        "folders": [serialize_folder(f) for f in MediaFolder.objects.for_workspace(workspace)],
        "next_cursor": _encode_cursor(page[-1]) if has_more and page else None,
    }
