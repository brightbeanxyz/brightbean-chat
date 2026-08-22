"""Django admin for the messaging spine.

Read-only throughout, and deliberately so. These tables are the record of what
strangers sent and what this deployment sent back: an admin who can edit them
can rewrite it, and the inbox (#14) is where an operator is supposed to act.

``Message.body`` never reaches a list column. It is attacker-controlled JSON
(SECURITY-BASELINE §2) and the admin's list rendering is one of the places a
stored payload would be displayed to a superuser with no escaping decision of
its own; ``ContactChannelIdentity.extra`` is excluded from lists for the same
reason. Both remain visible on the detail page, where Django renders a
``JSONField`` through a textarea widget that escapes.
"""

from typing import Any

from django.contrib import admin
from django.http import HttpRequest

from apps.messaging.models import ContactChannelIdentity, Conversation, Message


class ReadOnlyAdmin(admin.ModelAdmin):
    """No add, no change, no delete — the three permissions, all denied."""

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


@admin.register(ContactChannelIdentity)
class ContactChannelIdentityAdmin(ReadOnlyAdmin):
    list_display = (
        "platform_user_id",
        "platform",
        "workspace",
        "opt_in",
        "opt_in_source",
        "opted_out_at",
        "window_expires_at",
        "last_inbound_at",
    )
    list_filter = ("platform", "opt_in", "opt_in_source")
    # Not searchable by platform_user_id: on SMS and email that column *is* a
    # phone number or an address, and an admin search box is a poor place to put
    # one. Reach an identity through its contact.
    raw_id_fields = ("contact", "channel_connection", "workspace")
    readonly_fields = tuple(field.name for field in ContactChannelIdentity._meta.fields)


@admin.register(Conversation)
class ConversationAdmin(ReadOnlyAdmin):
    list_display = ("__str__", "workspace", "channel_connection", "state", "assignee", "last_message_at")
    list_filter = ("state",)
    raw_id_fields = ("contact", "channel_connection", "workspace", "assignee")
    readonly_fields = tuple(field.name for field in Conversation._meta.fields)


@admin.register(Message)
class MessageAdmin(ReadOnlyAdmin):
    list_display = ("__str__", "workspace", "direction", "source", "status", "error", "created_at")
    list_filter = ("direction", "source", "status", "internal")
    raw_id_fields = ("conversation", "channel_connection", "workspace")
    readonly_fields = tuple(field.name for field in Message._meta.fields)
