"""Admin registration.

These tables are registered, unlike the workspace-scoped ones in
``apps.credentials`` — nothing here is tenant data behind an enforcing manager,
and an operator debugging "the alert never arrived" needs to see the delivery
ledger.

Everything is read-only where it is a record of something that happened.
Editing a notification's title in the admin would rewrite history for the person
who received it.
"""

from typing import Any

from django.contrib import admin
from django.http import HttpRequest

from apps.notifications.models import Notification, NotificationDelivery, NotificationSetting


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("title", "user", "event_type", "is_read", "created_at")
    list_filter = ("event_type", "is_read", "created_at")
    search_fields = ("title", "body", "user__email")
    readonly_fields = ("id", "created_at", "updated_at")
    raw_id_fields = ("user",)

    def has_add_permission(self, request: HttpRequest) -> bool:
        # Notifications are produced by notify(), never hand-written: a row with
        # no event behind it is a lie to whoever receives it.
        return False


@admin.register(NotificationDelivery)
class NotificationDeliveryAdmin(admin.ModelAdmin):
    list_display = ("notification", "channel", "status", "attempts", "sent_at")
    list_filter = ("channel", "status")
    readonly_fields = ("id", "created_at", "updated_at")
    raw_id_fields = ("notification",)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


@admin.register(NotificationSetting)
class NotificationSettingAdmin(admin.ModelAdmin):
    list_display = ("user", "email_enabled")
    list_filter = ("email_enabled",)
    search_fields = ("user__email",)
    readonly_fields = ("id", "created_at", "updated_at")
    raw_id_fields = ("user",)
