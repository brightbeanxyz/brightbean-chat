"""Django admin for organization-level credentials.

Opening a change page decrypts secrets into an HTML response, so every
permission hook is superuser-only — ``is_staff`` (which the admin already
requires) is not a high enough bar for the crown jewels (SECURITY-BASELINE §5).

``WorkspaceCredentialOverride`` is deliberately **not** registered: it is
workspace-scoped tenant data with its own permission-gated UI, and an admin
listing would be a cross-tenant view of every workspace's secrets.
"""

from typing import Any

from django.contrib import admin
from django.http import HttpRequest

from apps.credentials.forms import PlatformCredentialAdminForm
from apps.credentials.models import PlatformCredential


@admin.register(PlatformCredential)
class PlatformCredentialAdmin(admin.ModelAdmin):
    form = PlatformCredentialAdminForm
    list_display = ("organization", "platform", "is_configured", "updated_at")
    list_filter = ("platform", "is_configured")
    search_fields = ("organization__name",)
    # is_configured is derived on save; hand-setting it would decouple the gate
    # from the values it is supposed to describe.
    readonly_fields = ("id", "is_configured", "created_at", "updated_at")

    def has_module_permission(self, request: HttpRequest) -> bool:
        return bool(request.user.is_superuser)

    def has_view_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return bool(request.user.is_superuser)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return bool(request.user.is_superuser)

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return bool(request.user.is_superuser)

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return bool(request.user.is_superuser)
