"""Read-mostly admin for support. Counters are derived, so they are not editable."""

from typing import Any

from django.contrib import admin

from apps.broadcasts.models import Broadcast, BroadcastRecipient


@admin.register(Broadcast)
class BroadcastAdmin(admin.ModelAdmin):
    list_display = ("name", "workspace", "channel_connection", "status", "scheduled_at", "finished_at")
    list_filter = ("status",)
    search_fields = ("name",)
    # Every one of these is written by the composer or the fanout under a lock;
    # hand-editing one here would desynchronise the queue rows from the row they
    # describe.
    readonly_fields = ("stats", "started_at", "finished_at", "flow", "flow_version")

    def get_queryset(self, request: Any) -> Any:
        # Django's admin goes through _default_manager, which for a
        # WorkspaceScopedModel is the plain one. That is the intended path here:
        # a superuser looking at every tenant is what the admin is for.
        return super().get_queryset(request).select_related("workspace", "channel_connection")


@admin.register(BroadcastRecipient)
class BroadcastRecipientAdmin(admin.ModelAdmin):
    list_display = ("broadcast", "contact", "status", "reason")
    list_filter = ("status",)
    readonly_fields = ("broadcast", "contact", "identity", "message", "status", "reason")
