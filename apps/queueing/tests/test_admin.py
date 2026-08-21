"""The ops changelist: who may see it, and what "retry now" does."""

from datetime import timedelta
from typing import Any

import pytest
from django.contrib.admin.sites import AdminSite
from django.urls import reverse
from django.utils import timezone

from apps.queueing.admin import ScheduledActionAdmin
from apps.queueing.models import ActionStatus, ScheduledAction
from apps.queueing.tests.support import make_action
from tests.support import Tenancy, create_user

CHANGELIST = "admin:queueing_scheduledaction_changelist"


@pytest.fixture
def admin_instance() -> ScheduledActionAdmin:
    return ScheduledActionAdmin(ScheduledAction, AdminSite())


@pytest.mark.django_db
class TestPermissions:
    def test_a_staff_user_cannot_see_the_queue(self, client_for: Any) -> None:
        """The changelist reads through all_objects, so it spans every tenant.

        ``is_staff`` — which the admin already requires — is not a high enough
        bar for that, the same reasoning PlatformCredentialAdmin uses.
        """
        staff = create_user("staff@example.test", is_staff=True)
        response = client_for(staff).get(reverse(CHANGELIST))
        assert response.status_code == 403

    def test_a_superuser_can(self, client_for: Any, tenancy: Tenancy) -> None:
        superuser = create_user("root@example.test", is_staff=True, is_superuser=True)
        make_action(tenancy.workspace, type="visible_action")

        response = client_for(superuser).get(reverse(CHANGELIST))

        assert response.status_code == 200
        assert "visible_action" in response.content.decode()

    def test_rows_cannot_be_added_or_deleted(self, admin_instance: ScheduledActionAdmin, rf: Any) -> None:
        """A hand-typed payload is at best inert; cancelled beats deleted."""
        request = rf.get("/")
        request.user = create_user("root2@example.test", is_staff=True, is_superuser=True)

        assert admin_instance.has_add_permission(request) is False
        assert admin_instance.has_delete_permission(request) is False
        # Change stays on, or the changelist loses its actions dropdown along
        # with the edit form — every field is read-only anyway.
        assert admin_instance.has_change_permission(request) is True


@pytest.mark.django_db
class TestRetryNow:
    def test_it_requeues_a_failed_action_with_a_fresh_budget(
        self, admin_instance: ScheduledActionAdmin, rf: Any, tenancy: Tenancy
    ) -> None:
        action = make_action(
            tenancy.workspace,
            status=ActionStatus.FAILED,
            attempts=5,
            last_error="RuntimeError: boom",
            run_at=timezone.now() + timedelta(days=1),
        )
        request = _admin_request(rf)

        admin_instance.retry_now(request, ScheduledAction.objects.unscoped().filter(pk=action.pk))

        action.refresh_from_db()
        assert action.status == ActionStatus.PENDING
        # Reset, not preserved: an operator clicking retry has usually just
        # fixed the cause, and one attempt left would fail straight back.
        assert action.attempts == 0
        assert action.last_error == ""
        assert action.run_at <= timezone.now()

    def test_it_skips_rows_a_worker_is_holding(
        self, admin_instance: ScheduledActionAdmin, rf: Any, tenancy: Tenancy
    ) -> None:
        """Flipping a running row to pending would hand its work to a second worker."""
        running = make_action(tenancy.workspace, status=ActionStatus.RUNNING, attempts=1)
        failed = make_action(tenancy.workspace, status=ActionStatus.FAILED, attempts=5)
        request = _admin_request(rf)

        admin_instance.retry_now(request, ScheduledAction.objects.unscoped().all())

        running.refresh_from_db()
        failed.refresh_from_db()
        assert running.status == ActionStatus.RUNNING
        assert running.attempts == 1
        assert failed.status == ActionStatus.PENDING

    def test_a_requeued_action_is_claimed_again(
        self, admin_instance: ScheduledActionAdmin, rf: Any, tenancy: Tenancy
    ) -> None:
        from apps.queueing.worker import claim_batch

        action = make_action(tenancy.workspace, status=ActionStatus.FAILED, attempts=5)
        admin_instance.retry_now(_admin_request(rf), ScheduledAction.objects.unscoped().filter(pk=action.pk))
        # Postgres' now() is the transaction start time and this test runs
        # inside one; production claims run in autocommit. See test_housekeeping.
        ScheduledAction.objects.unscoped().filter(pk=action.pk).update(run_at=timezone.now() - timedelta(seconds=1))

        assert [row.pk for row in claim_batch()] == [action.pk]


def _admin_request(rf: Any) -> Any:
    from django.contrib.messages.storage.fallback import FallbackStorage

    request = rf.post("/")
    request.user = create_user(f"admin-{timezone.now().timestamp()}@example.test", is_staff=True, is_superuser=True)
    request.session = {}
    request._messages = FallbackStorage(request)
    return request
