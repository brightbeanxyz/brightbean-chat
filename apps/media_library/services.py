"""Every write to the library goes through here.

One chokepoint per operation, so the upload policy — sniff, then size, then
quota — is stated once and cannot drift between the browser surface and the
picker-driven surfaces that later issues add. Studio learned the same lesson the
hard way and says so in its own ``create_asset`` docstring; the difference here
is that the ordering is explicit and the quota is taken under a row lock.

Storage writes happen *before* the database transaction, not inside it. The
alternative — holding the workspace row lock across an S3 PUT — serialises every
upload in a workspace behind the slowest one, and a 200 MB video makes that
visible. The cost of this ordering is a possible orphaned object when the quota
check rejects, so the reject path deletes it, best-effort and without letting a
cleanup failure mask the real error.
"""

import contextlib
import logging
from typing import Any

from django.core.exceptions import ValidationError
from django.core.files.storage import default_storage
from django.db import transaction

from apps.media_library.mimes import MediaKind, UnsupportedMediaError, kind_for, sniff
from apps.media_library.models import MediaAsset, MediaFolder
from apps.media_library.quotas import QuotaExceededError, check_file_size, check_workspace_quota, largest_upload_bytes
from apps.media_library.storage import asset_upload_to, thumbnail_upload_to
from apps.media_library.thumbnails import make_thumbnail

logger = logging.getLogger(__name__)

# UnsupportedMediaError and QuotaExceededError are re-exported so a caller
# catches every upload failure from one module.
__all__ = [
    "QuotaExceededError",
    "UnsupportedMediaError",
    "create_asset",
    "create_folder",
    "delete_asset",
    "delete_folder",
    "move_asset",
    "rename_folder",
    "update_asset",
]


def create_asset(
    *,
    workspace: Any,
    uploaded_file: Any,
    uploaded_by: Any,
    folder: MediaFolder | None = None,
    title: str = "",
    alt_text: str = "",
) -> MediaAsset:
    """Validate, store and register one uploaded file.

    Raises :class:`~apps.media_library.mimes.UnsupportedMediaError` for a type
    outside the allowlist and
    :class:`~apps.media_library.quotas.QuotaExceededError` for either size
    limit. Both carry a message meant for the uploader.
    """
    declared_size = int(getattr(uploaded_file, "size", 0) or 0)

    # Cheapest reject first: larger than every per-kind cap means no allowed
    # type could accept it, so there is no reason to read a byte.
    if declared_size > largest_upload_bytes():
        raise QuotaExceededError("That file is larger than any media type this deployment accepts.")

    # The client's Content-Type header and the filename extension are not
    # consulted, here or anywhere else in this app.
    mime = sniff(uploaded_file)
    kind = kind_for(mime)
    check_file_size(kind, declared_size)

    asset = MediaAsset(
        workspace=workspace,
        folder=folder,
        uploaded_by=uploaded_by,
        filename=_clean_filename(uploaded_file.name),
        kind=kind,
        mime=mime,
        size=declared_size,
        title=title.strip()[:255],
        alt_text=alt_text.strip(),
    )

    thumbnail = make_thumbnail(uploaded_file) if kind == MediaKind.IMAGE else None
    if thumbnail is not None:
        asset.width, asset.height = thumbnail.width, thumbnail.height

    # Everything from the first byte written onwards is inside the guard. The
    # thumbnail write used to sit above it, so a failure there — a full disk, a
    # throttled bucket — propagated with the original already stored and no row
    # pointing at it. There is no orphan sweep in this app to find it later.
    written: list[str] = []
    try:
        # Assigning a *string* to a FileField leaves it "committed", so the save()
        # below writes only the path column instead of re-uploading the bytes.
        asset.file = _write(written, asset_upload_to(asset, uploaded_file.name), uploaded_file)

        if thumbnail is not None and thumbnail.content is not None:
            asset.thumbnail = _write(written, thumbnail_upload_to(asset, ""), thumbnail.content)
            asset.thumbnail_size = thumbnail.content.size

        with transaction.atomic():
            _lock_workspace(workspace)
            # The thumbnail counts too: it is bytes this upload put in the same
            # bucket, and a quota that ignores them bills the operator for storage
            # its own usage bar says is free.
            check_workspace_quota(workspace, declared_size + asset.thumbnail_size)
            asset.save()
    except Exception:
        for orphan in written:
            with contextlib.suppress(Exception):
                default_storage.delete(orphan)
        raise

    return asset


def _write(written: list[str], name: str, content: Any) -> str:
    """Store one object, remembering it so a later failure can undo it."""
    stored = default_storage.save(name, content)
    written.append(stored)
    return stored


def _lock_workspace(workspace: Any) -> None:
    """Serialise quota accounting for one workspace.

    Without the lock, two concurrent uploads both read the pre-upload total,
    both find room, and both commit — which is how a soft quota becomes no quota
    under exactly the concurrency that makes it matter.
    """
    from apps.workspaces.models import Workspace

    Workspace.objects.select_for_update().filter(pk=workspace.pk).first()


def _clean_filename(name: str) -> str:
    """Keep the uploader's name for display, stripped of anything structural.

    The name never reaches a storage path (see
    :func:`apps.media_library.storage.asset_upload_to`), but it does reach a
    ``Content-Disposition`` header, so the separators and control characters
    that make that header ambiguous come off here as well as there.
    """
    base = (name or "file").replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = "".join(char for char in base if char.isprintable())
    return (cleaned or "file")[:255]


def update_asset(asset: MediaAsset, *, title: str | None = None, alt_text: str | None = None) -> MediaAsset:
    fields = []
    if title is not None:
        asset.title = title.strip()[:255]
        fields.append("title")
    if alt_text is not None:
        asset.alt_text = alt_text.strip()
        fields.append("alt_text")
    if fields:
        asset.save(update_fields=[*fields, "updated_at"])
    return asset


def move_asset(asset: MediaAsset, folder: MediaFolder | None) -> MediaAsset:
    """Move an asset into ``folder``, or to the library root when ``None``."""
    if folder is not None and folder.workspace_id != asset.workspace_id:
        raise ValidationError("That folder belongs to a different workspace.")
    asset.folder = folder
    asset.save(update_fields=["folder", "updated_at"])
    return asset


def delete_asset(asset: MediaAsset) -> None:
    """Delete the row first, then the bytes.

    Order matters. The row is what ``resolve`` reads, so removing it is what
    actually invalidates every delivery URL already handed to a platform; a
    storage delete that fails afterwards costs money, while a row that survives
    a successful storage delete would keep resolving to bytes that are gone.
    """
    names = [asset.file.name, asset.thumbnail.name]  # the same objects _write tracked
    asset.delete()
    for name in names:
        if name:
            with contextlib.suppress(Exception):
                default_storage.delete(name)


def create_folder(*, workspace: Any, name: str, parent: MediaFolder | None = None) -> MediaFolder:
    from django.conf import settings

    if parent is not None and parent.workspace_id != workspace.pk:
        raise ValidationError("That folder belongs to a different workspace.")

    # Depth is capped by MediaFolder.clean(); this caps breadth. Without it the
    # picker payload, the move dropdown and the sidebar rail each render an
    # unbounded list, and they all grow together.
    limit = int(settings.MEDIA_MAX_FOLDERS_PER_WORKSPACE)
    if MediaFolder.objects.for_workspace(workspace).count() >= limit:
        raise ValidationError(f"This workspace already has the maximum of {limit} folders.")
    folder = MediaFolder(workspace=workspace, parent=parent, name=name.strip()[:255])
    # No ``exclude`` here, deliberately. Both uniqueness constraints are on
    # (workspace, ...), and Django skips validating any constraint that touches
    # an excluded field — so excluding ``workspace`` would turn a duplicate name
    # from a ValidationError the view can render into an IntegrityError 500.
    folder.full_clean()
    folder.save()
    return folder


def rename_folder(folder: MediaFolder, name: str) -> MediaFolder:
    folder.name = name.strip()[:255]
    folder.full_clean()
    folder.save(update_fields=["name", "updated_at"])
    return folder


def delete_folder(folder: MediaFolder) -> None:
    """Remove a folder, lifting its contents one level rather than deleting them.

    ``on_delete=CASCADE`` on the self-referential parent would take the whole
    subtree with it, and assets are the expensive thing in it. Studio reparents
    for the same reason.
    """
    with transaction.atomic():
        MediaFolder.objects.for_workspace(folder.workspace_id).filter(parent=folder).update(parent=folder.parent)
        MediaAsset.objects.for_workspace(folder.workspace_id).filter(folder=folder).update(folder=folder.parent)
        folder.delete()
