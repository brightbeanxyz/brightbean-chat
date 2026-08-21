"""Admin for flows.

Read-mostly on purpose. Version numbering and the one-published-version rule are
enforced by :mod:`apps.flows.services` inside a row lock; an admin form that let
someone tick ``published`` on a second row would be editing around that lock, and
the database would answer with an IntegrityError rather than anything useful.
"""

from typing import Any

from django.contrib import admin

from apps.flows.models import Flow, FlowVersion


@admin.register(Flow)
class FlowAdmin(admin.ModelAdmin):
    list_display = ("name", "workspace", "status", "folder", "updated_at")
    list_filter = ("status",)
    search_fields = ("name", "folder")
    # The plain manager, which is what ModelAdmin uses anyway: the admin is
    # cross-tenant by design and the scoped manager would refuse to run here.
    ordering = ("workspace", "name")


@admin.register(FlowVersion)
class FlowVersionAdmin(admin.ModelAdmin):
    list_display = ("flow", "version", "published", "created_by", "updated_at")
    list_filter = ("published",)
    readonly_fields = ("flow", "version", "graph_json", "published", "created_by")

    def has_add_permission(self, request: Any, obj: Any = None) -> bool:
        return False
