"""Admin registration.

Read-mostly and deliberately thin. The admin goes through ``_default_manager``,
which on a ``WorkspaceScopedModel`` is the plain ``all_objects`` — that is what
makes cross-tenant listing work here and nowhere else, and it is a superuser
surface, not an application one.
"""

from django.contrib import admin

from apps.media_library.models import MediaAsset, MediaFolder


@admin.register(MediaFolder)
class MediaFolderAdmin(admin.ModelAdmin):
    list_display = ("name", "workspace", "parent", "created_at")
    list_select_related = ("workspace", "parent")
    search_fields = ("name",)


@admin.register(MediaAsset)
class MediaAssetAdmin(admin.ModelAdmin):
    list_display = ("filename", "workspace", "kind", "mime", "size", "created_at")
    list_select_related = ("workspace", "folder")
    list_filter = ("kind",)
    search_fields = ("filename", "title")
    # The stored mime is a security-relevant value derived from the bytes; an
    # admin who could retype it could re-open the stored-XSS door the sniffer
    # closed.
    readonly_fields = ("mime", "kind", "size", "width", "height", "file", "thumbnail")
