"""Size limits, per file and per workspace (SECURITY-BASELINE §9).

Studio resolves a per-*organization* cap through subscription tiers and an
override row in a settings table. None of that machinery exists here and none of
it is wanted: this is a self-hostable product, so the limits are environment
variables with defaults that fit a small box, and the tenant boundary they are
counted against is the workspace — the same boundary everything else in the app
is scoped to.

Two independent limits, because they stop different things. The per-file cap
(by kind, since a video is legitimately larger than an avatar) bounds what a
single request can cost. The per-workspace cap bounds what a member can
accumulate over a thousand of them.
"""

from typing import Any

from django.conf import settings
from django.db.models import Sum

from apps.media_library.mimes import MediaKind

__all__ = ["QuotaExceededError", "max_upload_bytes", "used_bytes", "workspace_quota_bytes"]

# Keyed by the stored ``kind`` string: what comes off a model field is the
# value, not the enum member, and the two are only interchangeable by luck.
_KIND_SETTING: dict[str, str] = {
    MediaKind.IMAGE: "MEDIA_MAX_UPLOAD_BYTES_IMAGE",
    MediaKind.AUDIO: "MEDIA_MAX_UPLOAD_BYTES_AUDIO",
    MediaKind.VIDEO: "MEDIA_MAX_UPLOAD_BYTES_VIDEO",
    MediaKind.FILE: "MEDIA_MAX_UPLOAD_BYTES_FILE",
}


class QuotaExceededError(Exception):
    """A limit was hit. The message is shown to the uploader verbatim."""


def max_upload_bytes(kind: str) -> int:
    return int(getattr(settings, _KIND_SETTING[kind]))


def largest_upload_bytes() -> int:
    """The most generous per-kind cap.

    Used for the cheap pre-sniff reject: a file larger than *any* kind's limit
    cannot become a valid asset, so it can be refused before its bytes are read.
    """
    return max(int(getattr(settings, name)) for name in _KIND_SETTING.values())


def workspace_quota_bytes() -> int:
    return int(settings.MEDIA_WORKSPACE_QUOTA_BYTES)


def used_bytes(workspace: Any) -> int:
    """Bytes currently stored for ``workspace``, thumbnails included.

    A live aggregate rather than a denormalised counter. At the scale this
    product targets that is one indexed sum, and a counter is a second source of
    truth that drifts the first time a delete path forgets to decrement it.

    Both columns, because both are objects in the bucket. Summing ``size`` alone
    would report a workspace of twenty thousand images as comfortably inside an
    allowance it had already spent.
    """
    from apps.media_library.models import MediaAsset

    totals = MediaAsset.objects.for_workspace(workspace).aggregate(
        files=Sum("size"),
        thumbnails=Sum("thumbnail_size"),
    )
    return int(totals["files"] or 0) + int(totals["thumbnails"] or 0)


def _mb(value: int) -> str:
    return f"{value / (1024 * 1024):.0f} MB"


def check_file_size(kind: str, size: int) -> None:
    limit = max_upload_bytes(kind)
    if size > limit:
        raise QuotaExceededError(f"That {kind} is {_mb(size)}. The limit for {kind} files is {_mb(limit)}.")


def check_workspace_quota(workspace: Any, incoming: int) -> None:
    """Refuse an upload that would push the workspace over its cap.

    Callers must hold the workspace row lock (see
    ``apps.media_library.services.create_asset``): read-then-write without one
    lets two concurrent uploads each observe the pre-upload total and both pass.
    """
    limit = workspace_quota_bytes()
    used = used_bytes(workspace)
    if used + max(incoming, 0) > limit:
        raise QuotaExceededError(
            f"This workspace has used {_mb(used)} of its {_mb(limit)} media allowance. "
            f"Delete something before uploading {_mb(incoming)}."
        )
