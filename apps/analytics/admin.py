"""Read-only admin for support (issue #26).

Every counter here is derived from a send or a click, and there is no path that
would reconcile a hand-edited one back to the events behind it — so the whole row
is read-only. The tracking toggles are editable because they *are* configuration;
they are also on a settings page, and this is the copy an operator reaches when
they are debugging why an email carried a pixel.
"""

from typing import Any

from django.contrib import admin

from apps.analytics.models import NodeStatDaily, TrackingSettings


@admin.register(NodeStatDaily)
class NodeStatDailyAdmin(admin.ModelAdmin):
    list_display = ("date", "workspace", "flow", "node_id", "sent", "delivered", "failed", "clicked")
    list_filter = ("date",)
    search_fields = ("node_id",)
    readonly_fields = ("workspace", "flow", "node_id", "date", "sent", "delivered", "failed", "clicked")

    def has_add_permission(self, request: Any) -> bool:
        # A counter with no send behind it is a lie, and the upsert is the only
        # thing that should ever write one.
        return False

    def get_queryset(self, request: Any) -> Any:
        # Django's admin goes through _default_manager, which for a
        # WorkspaceScopedModel is the plain one. That is the intended path here:
        # a superuser looking at every tenant is what the admin is for.
        return super().get_queryset(request).select_related("workspace", "flow")


@admin.register(TrackingSettings)
class TrackingSettingsAdmin(admin.ModelAdmin):
    list_display = ("workspace", "wrap_email_links", "open_pixel")
    list_filter = ("wrap_email_links", "open_pixel")

    def get_queryset(self, request: Any) -> Any:
        return super().get_queryset(request).select_related("workspace")
