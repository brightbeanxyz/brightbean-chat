"""Operator visibility for the queue.

Superuser-only, matching ``PlatformCredentialAdmin``'s precedent, and for the
same structural reason rather than a stylistic one: the admin reads through
``_default_manager``, which on a ``WorkspaceScopedModel`` is the *plain*
``all_objects`` manager (``apps.common.scoping``). So this changelist is a
cross-tenant view of every workspace's queued work, and ``is_staff`` — which the
admin already requires — is not a high enough bar for that.

Add and delete are off. A queued row is created by ``schedule()`` with an
idempotency key and a payload some handler knows how to read; one typed into a
form is at best inert and at worst a way to make the worker run something with a
hand-written payload. Deleting is worse than useless — ``cancelled`` exists, and
it leaves the audit trail behind.
"""

import logging
from typing import Any

from django.contrib import admin, messages
from django.db.models import QuerySet
from django.http import HttpRequest
from django.utils import timezone

from apps.queueing.models import ActionStatus, ScheduledAction

logger = logging.getLogger(__name__)


@admin.register(ScheduledAction)
class ScheduledActionAdmin(admin.ModelAdmin):
    list_display = ("id", "type", "status", "run_at", "attempts", "max_attempts", "workspace", "updated_at")
    list_filter = ("status", "type")
    # Not the UUID columns: Postgres has no icontains for uuid, so adding
    # `id` or `contact_id` here turns every admin search into a 500.
    search_fields = ("idempotency_key",)
    ordering = ("-run_at",)
    date_hierarchy = "run_at"
    readonly_fields = (
        "id",
        "workspace",
        "contact_id",
        "type",
        "payload",
        "status",
        "attempts",
        "max_attempts",
        "last_error",
        "idempotency_key",
        "run_at",
        "created_at",
        "updated_at",
    )
    actions = ("retry_now",)

    @admin.action(description="Retry now (reset attempts and run immediately)")
    def retry_now(self, request: HttpRequest, queryset: QuerySet[ScheduledAction]) -> None:
        """Put the selected rows back at the front of the queue.

        ``attempts`` is reset to zero, not preserved: an operator clicking
        retry has usually just fixed the thing that was failing, and a row
        that came back with one attempt left would fail again and stay failed.
        A ``running`` row is skipped — a worker may be inside its handler right
        now, and flipping it to ``pending`` underneath would hand the same work
        to a second worker.
        """
        eligible = queryset.exclude(status=ActionStatus.RUNNING)
        updated = eligible.update(
            status=ActionStatus.PENDING,
            run_at=timezone.now(),
            attempts=0,
            last_error="",
            updated_at=timezone.now(),
        )
        skipped = queryset.count() - updated
        logger.warning("Admin %s requeued %s scheduled action(s)", request.user, updated)
        self.message_user(request, f"Requeued {updated} action(s).", messages.SUCCESS)
        if skipped:
            self.message_user(
                request,
                f"Skipped {skipped} action(s) that are currently running.",
                messages.WARNING,
            )

    # -- permissions --------------------------------------------------------

    def has_module_permission(self, request: HttpRequest) -> bool:
        return bool(request.user.is_superuser)

    def has_view_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return bool(request.user.is_superuser)

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        # False would hide the changelist's action dropdown along with the edit
        # form, taking "retry now" with it. Every field is read-only, so the
        # change page is a detail view that happens to have a Save button.
        return bool(request.user.is_superuser)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False
