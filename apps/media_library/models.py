"""Folders and assets — both tenant models (SPEC §5, SECURITY-BASELINE §1).

Studio's ``MediaAsset`` carries a nullable ``workspace`` so an organization can
hold a shared library above its workspaces. That shape cannot survive here:
:class:`~apps.common.scoping.WorkspaceScopedModel`'s ``workspace`` foreign key
is not nullable, and ``for_workspace(None)`` raises rather than quietly
filtering on ``IS NULL``. The org tier is therefore dropped (it is an agency
feature, and all four downstream consumers — the flow builder, broadcasts, the
inbox and the email editor — are workspace-scoped), and every asset belongs to
exactly one workspace, inside the enforcing manager.

Also not ported, because issue #16 does not ask for them: version history, the
crop/rotate/trim editor, starring, Unsplash attribution and the per-platform
``processed_variants`` blob. What is kept is the part the send path needs — a
stable id, the real content type, and enough metadata to render a picker.
"""

import logging

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.storage import default_storage
from django.db import models, transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver

from apps.common.scoping import WorkspaceScopedModel
from apps.media_library.mimes import MediaKind
from apps.media_library.storage import asset_upload_to, thumbnail_upload_to

logger = logging.getLogger(__name__)

__all__ = ["MAX_FOLDER_DEPTH", "MediaAsset", "MediaFolder"]

#: Studio's limit, kept. Deep trees are a browsing problem long before they are
#: a storage one, and a bounded depth means the ancestor walk below terminates.
MAX_FOLDER_DEPTH = 3


class MediaFolder(WorkspaceScopedModel):
    """A folder in one workspace's library. Nested at most three levels."""

    parent = models.ForeignKey(
        "self",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="children",
    )
    name = models.CharField(max_length=255)

    class Meta:
        db_table = "media_library_folder"
        ordering = ["name"]
        constraints = [
            # violation_error_message on both, because these messages are shown
            # to whoever typed the name. Django's default renders the constraint
            # identifier verbatim — "Constraint “media_folder_unique_root_name”
            # is violated." — which names a database object the reader has no
            # way to act on.
            models.UniqueConstraint(
                fields=["workspace", "parent", "name"],
                name="media_folder_unique_name_per_parent",
                violation_error_message="A folder with that name already exists here.",
            ),
            # A NULL parent is a NULL in the constraint above, and NULLs never
            # collide in SQL — so without this second constraint the whole root
            # level of every library accepts duplicate names while the nested
            # levels do not.
            models.UniqueConstraint(
                fields=["workspace", "name"],
                condition=models.Q(parent__isnull=True),
                name="media_folder_unique_root_name",
                violation_error_message="A folder with that name already exists.",
            ),
        ]

    def __str__(self) -> str:
        return self.name

    @property
    def depth(self) -> int:
        """Distance from the root, counting the root as 0."""
        depth = 0
        current = self.parent
        while current is not None and depth <= MAX_FOLDER_DEPTH:
            depth += 1
            current = current.parent
        return depth

    def clean(self) -> None:
        super().clean()
        if self.parent is not None and self.parent.depth + 1 >= MAX_FOLDER_DEPTH:
            raise ValidationError(f"Folders cannot be nested more than {MAX_FOLDER_DEPTH} levels deep.")


class MediaAsset(WorkspaceScopedModel):
    """One uploaded file, addressable by id from a ``send_message`` block."""

    folder = models.ForeignKey(
        MediaFolder,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assets",
    )
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="uploaded_media",
    )

    file = models.FileField(upload_to=asset_upload_to, max_length=512)
    # A FileField rather than an ImageField: this app writes the thumbnail
    # itself and already knows it is a JPEG, and ImageField would add a Pillow
    # system check to a model that otherwise imports fine without it.
    thumbnail = models.FileField(upload_to=thumbnail_upload_to, max_length=512, blank=True)

    #: The name the uploader's browser sent. Display and download only — it is
    #: never part of a storage path and never decides a content type.
    filename = models.CharField(max_length=255)
    kind = models.CharField(max_length=10, choices=MediaKind.choices)
    #: From apps.media_library.mimes.sniff, never from the client's header.
    mime = models.CharField(max_length=100)
    size = models.PositiveBigIntegerField(default=0)
    #: Bytes the generated thumbnail occupies. Counted separately from ``size``
    #: because ``size`` is the asset's own length — what the platform ceilings in
    #: platform_limits.py are compared against — while the quota has to account
    #: for everything this upload actually put in the bucket.
    thumbnail_size = models.PositiveBigIntegerField(default=0)

    width = models.PositiveIntegerField(default=0)
    height = models.PositiveIntegerField(default=0)

    title = models.CharField(max_length=255, blank=True)
    alt_text = models.TextField(blank=True)

    class Meta:
        db_table = "media_library_asset"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["workspace", "kind", "-created_at"], name="media_asset_ws_kind_idx"),
            models.Index(fields=["folder"], name="media_asset_folder_idx"),
        ]

    def __str__(self) -> str:
        return self.filename

    @property
    def is_image(self) -> bool:
        return self.kind == MediaKind.IMAGE

    @property
    def size_display(self) -> str:
        size = float(self.size)
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024:
                return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"


@receiver(post_delete, sender=MediaAsset)
def _delete_stored_objects(sender: type[MediaAsset], instance: MediaAsset, **kwargs: object) -> None:
    """Remove an asset's bytes whenever its row goes, by whatever route.

    ``FileField`` has not deleted its own file since Django 1.3, and the service
    layer's ``delete_asset`` only covers the one path that calls it. Rows also
    disappear through cascades — ``Organization.hard_delete()`` reaches these
    through workspaces — and through the admin and any queryset ``.delete()``.
    Every one of those left originals and thumbnails in the bucket forever, with
    no row to find them by and no orphan sweep in this app to go looking. On the
    org-deletion path that is not a storage bill, it is user data surviving a
    deletion the product told someone had happened.

    ``on_commit`` because the bytes must not go before the row's removal is
    durable: a transaction that rolls back after this fires would otherwise
    leave a live row pointing at nothing. Outside a transaction Django runs the
    callback immediately, so the plain path is unchanged.

    Best-effort by design. A storage backend that is down must not turn a delete
    into a 500 — the row is already gone, which is what invalidates the delivery
    URLs, and a leaked object is the lesser failure.
    """
    names = [name for name in (instance.file.name, instance.thumbnail.name) if name]
    if not names:
        return

    def remove() -> None:
        for name in names:
            try:
                default_storage.delete(name)
            except Exception:
                logger.warning("Could not delete stored media object %s", name, exc_info=True)

    transaction.on_commit(remove)
