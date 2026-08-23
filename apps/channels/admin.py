"""Django admin for channels.

Read-mostly on purpose. The admin is a superuser tool for looking at what a
deployment holds, not a second connect flow — and everything sensitive is
excluded from it: ``credentials`` and ``webhook_secret`` are encrypted fields
whose admin widget would decrypt and render them straight into an HTML page
(SECURITY-BASELINE §5).

The event log is fully read-only. Its rows are the audit trail for what a
platform actually sent; an admin who can edit them can rewrite it.
"""

from typing import Any

from django.contrib import admin
from django.http import HttpRequest

from apps.channels.models import ChannelConnection, WebhookEventLog, WhatsAppCostHint, WhatsAppTemplate


@admin.register(ChannelConnection)
class ChannelConnectionAdmin(admin.ModelAdmin):
    list_display = ("display_name", "platform", "workspace", "status", "external_id", "updated_at")
    list_filter = ("platform", "status")
    search_fields = ("display_name", "external_id")
    # Never in a form, never in a list: both decrypt on access.
    exclude = ("credentials", "webhook_secret", "webhook_secret_digest")
    readonly_fields = ("capabilities_cache", "created_at", "updated_at")


@admin.register(WebhookEventLog)
class WebhookEventLogAdmin(admin.ModelAdmin):
    list_display = ("provider_event_id", "platform", "connection", "status", "received_at", "processed_at")
    list_filter = ("platform", "status")
    search_fields = ("provider_event_id",)
    readonly_fields = tuple(field.name for field in WebhookEventLog._meta.fields)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


@admin.register(WhatsAppTemplate)
class WhatsAppTemplateAdmin(admin.ModelAdmin):
    """Read-mostly, like the rest of this module (issue #19).

    ``status`` and ``rejected_reason`` are **read-only**, and that is the point
    rather than caution: they are Meta's verdict, written by the hourly poll,
    and a template a superuser marked approved by hand would be one the
    compliance engine lets out and Meta then refuses. The place to change a
    template's state is Meta.
    """

    list_display = ("name", "language", "category", "status", "workspace", "updated_at")
    list_filter = ("status", "category")
    search_fields = ("name", "meta_template_id")
    readonly_fields = ("status", "rejected_reason", "meta_template_id", "created_at", "updated_at")


@admin.register(WhatsAppCostHint)
class WhatsAppCostHintAdmin(admin.ModelAdmin):
    """Per-workspace price estimates. Display only — nothing meters (SPEC §22)."""

    list_display = ("workspace", "currency", "marketing", "utility", "authentication", "updated_at")
    readonly_fields = ("created_at", "updated_at")
