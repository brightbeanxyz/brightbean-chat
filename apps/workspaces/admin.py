from django.contrib import admin

from apps.workspaces.models import Workspace


@admin.register(Workspace)
class WorkspaceAdmin(admin.ModelAdmin):
    list_display = ("name", "organization", "is_archived", "created_at")
    list_filter = ("is_archived",)
    search_fields = ("name", "organization__name")
    readonly_fields = ("id", "created_at", "updated_at")
