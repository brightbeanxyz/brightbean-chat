"""Admin permissions.

The module docstring in apps/notifications/admin.py claims these rows are
read-only records of things that happened. It was only half true: add was
disabled and change was not, so title, body and payload stayed editable.
"""

import pytest
from django.contrib import admin

from apps.notifications.models import Notification, NotificationDelivery


@pytest.mark.django_db
class TestRecordsOfWhatHappenedAreReadOnly:
    @pytest.mark.parametrize("model", [Notification, NotificationDelivery])
    def test_neither_can_be_added_or_changed(self, model, tenancy, rf):
        request = rf.get("/admin/")
        request.user = tenancy.owner
        site = admin.site._registry[model]

        assert site.has_add_permission(request) is False
        assert site.has_change_permission(request) is False

    def test_a_notification_row_is_still_visible_to_an_operator(self, tenancy, rf):
        """Read-only, not hidden — an operator debugging "the alert never
        arrived" needs to see the ledger. A superuser because that is who
        reaches the admin; ordinary members are denied by Django's own view
        permission, which is not this module's business."""
        operator = tenancy.owner
        operator.is_staff = True
        operator.is_superuser = True
        operator.save(update_fields=["is_staff", "is_superuser"])
        request = rf.get("/admin/")
        request.user = operator

        assert admin.site._registry[Notification].has_view_permission(request) is True
