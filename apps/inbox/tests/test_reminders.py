"""Reminders: scheduled through the queue, delivered as a notification (SPEC §14).

Issue #24's second acceptance criterion — "reminder fires → notification with
deep link; cancellation works" — plus the two things that make rescheduling
correct, both of which come from ``schedule()`` returning an existing row
*unchanged whatever its status* when a key is already present.

Driven through ``apps.queueing.worker.process_action`` rather than by calling the
handler: the transaction and the contact advisory lock the handler runs inside
are the worker's, and a test that called the function directly would be running
it in a world it never sees in production.
"""

from datetime import timedelta
from typing import Any

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.inbox import services
from apps.inbox.models import DeferredStatus, InboxReminder
from apps.notifications.models import Notification
from apps.queueing.models import ActionStatus, ScheduledAction
from apps.queueing.worker import process_action

pytestmark = pytest.mark.django_db


def _run_due() -> None:
    """Run every due action the way the worker does.

    ``.unscoped()`` with a reason, per CONTRIBUTING.md: the worker's claim is
    cross-tenant by design — it drains one queue for the whole deployment — so a
    test standing in for it has to look at the same rows.
    """
    for action in ScheduledAction.objects.unscoped().filter(status=ActionStatus.PENDING, run_at__lte=timezone.now()):
        process_action(action)


def _due_now(row: Any) -> Any:
    """Rewind a scheduled row to this instant.

    The service refuses a time in the past, correctly — an operator cannot set a
    reminder for yesterday. Production reaches "due" by waiting; a test reaches
    it by moving the clock hands, which is one UPDATE on each of the two rows
    rather than a fixture that freezes ``timezone.now``.
    """
    past = timezone.now() - timedelta(seconds=1)
    field = "remind_at" if hasattr(row, "remind_at") else "send_at"
    setattr(row, field, past)
    row.save(update_fields=[field, "updated_at"])
    ScheduledAction.objects.for_workspace(row.workspace_id).filter(pk=row.action_id).update(run_at=past)
    row.refresh_from_db()
    return row


def _schedule(conversation: Any, recipient: Any, **overrides: Any) -> InboxReminder:
    values: dict[str, Any] = {
        "recipient": recipient,
        "remind_at": timezone.now() + timedelta(minutes=5),
        "note": "chase the refund",
        "created_by": recipient,
    }
    values.update(overrides)
    return services.schedule_reminder(conversation, **values)


class TestScheduling:
    def test_it_arms_a_queue_row_naming_the_contact(self, tenancy, conversation):
        """The contact travels with the action so the worker takes that contact's
        advisory lock (SPEC §9.6) — and so ``contacts.activity.stand_down``
        cancels it when the contact is deleted, without knowing this table
        exists."""
        reminder = _schedule(conversation, tenancy.user_for("agent"))

        assert reminder.action is not None
        assert reminder.action.type == services.REMINDER
        assert reminder.action.contact_id == conversation.contact_id
        assert reminder.action.workspace_id == tenancy.workspace.pk

    def test_it_refuses_a_time_in_the_past(self, tenancy, conversation):
        with pytest.raises(services.InboxError):
            _schedule(conversation, tenancy.user_for("agent"), remind_at=timezone.now() - timedelta(minutes=1))


class TestFiring:
    def test_it_notifies_the_recipient_with_a_deep_link(self, tenancy, conversation):
        agent = tenancy.user_for("agent")
        reminder = _schedule(conversation, agent)
        _due_now(reminder)

        _run_due()

        notification = Notification.objects.filter(user=agent, event_type="inbox_reminder").get()
        assert reminder.conversation.contact.display_name in notification.title
        assert notification.payload["action_url"] == reverse(
            "inbox:thread",
            kwargs={"workspace_id": tenancy.workspace.pk, "conversation_id": conversation.pk},
        )
        reminder.refresh_from_db()
        assert reminder.status == DeferredStatus.SENT

    def test_running_it_twice_notifies_once(self, tenancy, conversation):
        """Zombie recovery resets a `running` row after ten minutes, so a slow
        handler can be claimed a second time. The status guard is what makes the
        re-run cheap — the same shape ``handle_send_retry`` opens with."""
        agent = tenancy.user_for("agent")
        reminder = _schedule(conversation, agent)
        _due_now(reminder)

        from apps.inbox.handlers import handle_reminder

        handle_reminder({"reminder_id": str(reminder.pk)}, reminder.action)
        handle_reminder({"reminder_id": str(reminder.pk)}, reminder.action)

        assert Notification.objects.filter(user=agent, event_type="inbox_reminder").count() == 1

    def test_a_recipient_who_left_falls_back_to_the_admins(self, tenancy, conversation):
        """``users=[]`` means *nobody* — the engine distinguishes it from ``None``
        deliberately — so a reminder for a departed member has to go somewhere.
        A reminder nobody receives is the same bug as one that never fired."""
        from apps.members.models import WorkspaceMembership

        agent = tenancy.user_for("agent")
        reminder = _schedule(conversation, agent)
        _due_now(reminder)
        WorkspaceMembership.objects.filter(workspace=tenancy.workspace, user=agent).delete()

        from apps.inbox.handlers import handle_reminder

        handle_reminder({"reminder_id": str(reminder.pk)}, reminder.action)

        assert not Notification.objects.filter(user=agent).exists()
        assert Notification.objects.filter(event_type="inbox_reminder").exists()

    def test_a_reminder_from_another_workspace_is_not_reachable(self, tenancy, other_tenancy, conversation):
        """A payload is a document that has been sitting in a table: its ids are
        ids, never trusted objects."""
        reminder = _schedule(conversation, tenancy.user_for("agent"))
        action = reminder.action
        action.workspace = other_tenancy.workspace
        action.save(update_fields=["workspace"])

        from apps.inbox.handlers import handle_reminder

        handle_reminder({"reminder_id": str(reminder.pk)}, action)

        reminder.refresh_from_db()
        assert reminder.status == DeferredStatus.PENDING
        assert not Notification.objects.filter(event_type="inbox_reminder").exists()


class TestCancellation:
    def test_it_cancels_the_queue_row_too(self, tenancy, conversation):
        reminder = _schedule(conversation, tenancy.user_for("agent"))

        assert services.cancel_reminder(reminder) is True

        reminder.refresh_from_db()
        reminder.action.refresh_from_db()
        assert reminder.status == DeferredStatus.CANCELLED
        assert reminder.action.status == ActionStatus.CANCELLED

    def test_a_cancelled_reminder_never_notifies(self, tenancy, conversation):
        agent = tenancy.user_for("agent")
        reminder = _schedule(conversation, agent)
        _due_now(reminder)
        services.cancel_reminder(reminder)

        _run_due()

        assert not Notification.objects.filter(user=agent, event_type="inbox_reminder").exists()

    def test_cancelling_twice_is_not_an_error(self, tenancy, conversation):
        reminder = _schedule(conversation, tenancy.user_for("agent"))

        assert services.cancel_reminder(reminder) is True
        assert services.cancel_reminder(reminder) is False


class TestWillFire:
    def test_a_soft_deleted_contact_stops_advertising_a_reminder(self, tenancy, conversation):
        """``contacts.services.delete_contact`` is a *soft* delete, so this row
        survives — while ``activity.stand_down`` cancels every pending action
        naming the contact. A row trusting only its own status column would
        advertise a reminder in the thread for ever."""
        from apps.contacts.activity import stand_down

        reminder = _schedule(conversation, tenancy.user_for("agent"))
        assert reminder.will_fire is True

        stand_down(conversation.contact)

        reminder.refresh_from_db()
        assert reminder.status == DeferredStatus.PENDING
        assert reminder.will_fire is False


class TestNotificationFailures:
    def test_a_failed_notification_retries_rather_than_reading_as_sent(self, tenancy, conversation, monkeypatch):
        """There is no provider call on this path to protect, so a notification
        backend having a bad day should take SPEC §15's backoff — swallowing it
        would mark the reminder SENT and lose it with nothing to retry."""
        from apps.inbox import handlers
        from apps.inbox.handlers import handle_reminder

        reminder = _schedule(conversation, tenancy.user_for("agent"))
        _due_now(reminder)
        monkeypatch.setattr(handlers, "_notify", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("down")))

        with pytest.raises(RuntimeError):
            handle_reminder({"reminder_id": str(reminder.pk)}, reminder.action)

        reminder.refresh_from_db()
        assert reminder.status == DeferredStatus.PENDING

    def test_the_worker_leaves_the_row_retriable(self, tenancy, conversation, monkeypatch):
        """And the queue does the rest: a raising handler comes back to pending
        with its run_at pushed out onto SPEC §15's ladder, rather than done."""
        from apps.inbox import handlers

        reminder = _schedule(conversation, tenancy.user_for("agent"))
        _due_now(reminder)
        monkeypatch.setattr(handlers, "_notify", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("down")))

        _run_due()

        reminder.action.refresh_from_db()
        assert reminder.action.status == ActionStatus.PENDING
        assert reminder.action.run_at > timezone.now()
        assert reminder.action.last_error


class TestASupersededAction:
    def test_a_superseded_action_does_not_notify(self, tenancy, conversation):
        """Same shape as the scheduled reply's: an action the row no longer
        points at must not fire, however it got claimed."""
        from apps.inbox.handlers import handle_reminder

        reminder = _schedule(conversation, tenancy.user_for("agent"))
        stale = reminder.action
        # Arm a second time, as a reschedule would.
        services._arm(reminder, services.REMINDER, reminder.remind_at, {"reminder_id": str(reminder.pk)})
        reminder.refresh_from_db()
        assert reminder.action_id != stale.pk

        handle_reminder({"reminder_id": str(reminder.pk)}, stale)

        assert not Notification.objects.filter(event_type="inbox_reminder").exists()
        reminder.refresh_from_db()
        assert reminder.status == DeferredStatus.PENDING
