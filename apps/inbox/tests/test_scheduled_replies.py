"""Scheduled replies: compose now, send later, fail visibly (SPEC §14).

Issue #24's third acceptance criterion — "scheduled reply sends at time, extends
pause, and fails visibly (with notification) when compliance denies at fire
time" — and the two ways a queue can run the same work twice.

The single hardest rule in the handler is that **nothing raises after the send**.
``send_as_agent``'s message row and its ``dispatched_at`` claim are savepoints
inside the handler's transaction, so an exception after the provider call has
gone out rolls both back while the message is already delivered — and the retry
sends it again. ``test_running_it_twice_sends_once`` is the standing proof.
"""

from datetime import timedelta
from typing import Any

import pytest
from django.utils import timezone

from apps.channels.events import OutboundMessage, TextBlock
from apps.channels.tests.fake_adapter import registered
from apps.common.platforms import Platform
from apps.inbox import services
from apps.inbox.handlers import handle_scheduled_reply
from apps.inbox.models import DeferredStatus, ScheduledReply
from apps.messaging.models import Conversation, ConversationState, Message, MessageSource, MessageStatus
from apps.messaging.services import record_opt_out
from apps.notifications.models import Notification
from apps.queueing.models import ActionStatus, ScheduledAction

pytestmark = pytest.mark.django_db

BODY = OutboundMessage(blocks=(TextBlock(text="the part you ordered ships monday"),)).to_body()


def _schedule(conversation: Conversation, by: Any, **overrides: Any) -> ScheduledReply:
    values: dict[str, Any] = {
        "body": BODY,
        "send_at": timezone.now() + timedelta(hours=2),
        "created_by": by,
    }
    values.update(overrides)
    return services.schedule_reply(conversation, **values)


def _due_now(reply: ScheduledReply) -> ScheduledReply:
    """Rewind the row and its queue action to this instant.

    The service refuses a time in the past, correctly. Production reaches "due"
    by waiting; a test moves the clock hands on the two rows instead.
    """
    past = timezone.now() - timedelta(seconds=1)
    reply.send_at = past
    reply.save(update_fields=["send_at", "updated_at"])
    assert reply.action_id is not None
    ScheduledAction.objects.for_workspace(reply.workspace_id).filter(pk=reply.action_id).update(run_at=past)
    reply.refresh_from_db()
    return reply


def _fire(reply: ScheduledReply) -> None:
    action = reply.action
    assert action is not None
    handle_scheduled_reply({"scheduled_reply_id": str(reply.pk)}, action)


def _sent(conversation: Conversation) -> Any:
    return Message.objects.for_workspace(conversation.workspace_id).filter(
        conversation=conversation, source=MessageSource.AGENT
    )


class TestSending:
    def test_it_sends_as_an_agent_when_it_comes_due(self, tenancy, conversation, identity):
        reply = _due_now(_schedule(conversation, tenancy.user_for("agent")))

        with registered(Platform.TELEGRAM) as adapter:
            _fire(reply)

        assert len(adapter.sends) == 1
        reply.refresh_from_db()
        assert reply.status == DeferredStatus.SENT
        assert reply.message is not None
        assert reply.message.source == MessageSource.AGENT

    def test_firing_it_extends_the_automation_pause(self, tenancy, conversation, identity):
        """No code here does this. ``send_outbound`` extends the pause for
        ``source="agent"``, so calling the facade *is* the implementation — and a
        scheduled reply is still an agent taking over."""
        reply = _due_now(_schedule(conversation, tenancy.user_for("agent")))
        assert conversation.automation_paused_until is None

        with registered(Platform.TELEGRAM):
            _fire(reply)

        conversation.refresh_from_db()
        assert conversation.automation_paused_until is not None
        assert conversation.automation_paused_until > timezone.now()

    def test_it_reopens_a_thread_that_was_marked_done(self, tenancy, conversation, identity):
        """``open_conversation`` is called on every send and reopens a done
        thread deliberately — "an outbound message on a closed conversation is
        an agent or an automation picking it back up"."""
        from apps.messaging.services import close_conversation

        close_conversation(conversation)
        reply = _due_now(_schedule(conversation, tenancy.user_for("agent")))

        with registered(Platform.TELEGRAM):
            _fire(reply)

        conversation.refresh_from_db()
        assert conversation.state == ConversationState.OPEN


class TestRunningItTwice:
    def test_zombie_recovery_sends_once(self, tenancy, conversation, identity):
        """A worker can die between the handler committing and the row being
        marked, and zombie recovery re-runs it after ten minutes. The status
        guard catches the ordinary case; the row-derived idempotency key catches
        the one where it does not."""
        reply = _due_now(_schedule(conversation, tenancy.user_for("agent")))

        with registered(Platform.TELEGRAM) as adapter:
            _fire(reply)
            _fire(reply)

        assert len(adapter.sends) == 1
        assert _sent(conversation).count() == 1

    def test_a_reschedule_still_sends_once(self, tenancy, conversation, identity):
        """The key is derived from **this row** and nothing else — not the
        action, not the attempt, not the time. A reschedule mints a new action
        for the same logical send, so both actions running has to collapse onto
        one Message through ``message_unique_conv_idem``."""
        reply = _schedule(conversation, tenancy.user_for("agent"))
        first_action = reply.action
        services.reschedule_reply(reply, body=BODY, send_at=timezone.now() + timedelta(hours=3))
        reply.refresh_from_db()
        second_action = reply.action

        assert first_action.pk != second_action.pk
        first_action.refresh_from_db()
        assert first_action.status == ActionStatus.CANCELLED

        _due_now(reply)
        with registered(Platform.TELEGRAM) as adapter:
            handle_scheduled_reply({"scheduled_reply_id": str(reply.pk)}, first_action)
            handle_scheduled_reply({"scheduled_reply_id": str(reply.pk)}, second_action)

        assert len(adapter.sends) == 1
        assert _sent(conversation).count() == 1


class TestComplianceAtFireTime:
    def test_a_denial_fails_visibly_and_notifies(self, tenancy, conversation, identity):
        """Never a silent drop. The row records the machine-readable code, the
        thread keeps the failed message, and the person who queued it is told."""
        agent = tenancy.user_for("agent")
        reply = _due_now(_schedule(conversation, agent))
        # The contact unsubscribed in the hours between composing and sending,
        # which is exactly why compliance is re-decided here rather than at
        # compose time. Through the facade, because ``opted_out_at`` has one
        # write site and ``record_opt_out`` is the door to it (contract 3).
        record_opt_out(identity, source="test")

        with registered(Platform.TELEGRAM) as adapter:
            _fire(reply)

        assert adapter.sends == []
        reply.refresh_from_db()
        assert reply.status == DeferredStatus.FAILED
        assert reply.error
        assert reply.message is not None
        assert reply.message.status == MessageStatus.FAILED
        assert Notification.objects.filter(user=agent, event_type="scheduled_reply_failed").exists()

    def test_a_denial_still_extends_the_pause(self, tenancy, conversation, identity):
        """``send_outbound`` extends the pause **before** compliance, and its
        comment says why: "the pause records an agent *taking over*, and a reply
        that compliance then refuses is still a takeover." Pinned here so nobody
        later reads it as a bug and compensates for it — which they could not do
        anyway, since that column has exactly one write site."""
        reply = _due_now(_schedule(conversation, tenancy.user_for("agent")))
        record_opt_out(identity, source="test")

        with registered(Platform.TELEGRAM):
            _fire(reply)

        conversation.refresh_from_db()
        assert conversation.automation_paused_until is not None

    def test_an_empty_body_fails_rather_than_retrying(self, tenancy, conversation, identity):
        """No number of retries gives an empty body something to send, and a
        reply the agent believed was queued has to end somewhere visible."""
        reply = _due_now(_schedule(conversation, tenancy.user_for("agent"), body={"blocks": []}))

        with registered(Platform.TELEGRAM) as adapter:
            _fire(reply)

        assert adapter.sends == []
        reply.refresh_from_db()
        assert reply.status == DeferredStatus.FAILED


class TestCancellation:
    def test_cancelling_stops_the_queue_row(self, tenancy, conversation, identity):
        reply = _schedule(conversation, tenancy.user_for("agent"))

        assert services.cancel_scheduled_reply(reply) is True

        reply.refresh_from_db()
        reply.action.refresh_from_db()
        assert reply.status == DeferredStatus.CANCELLED
        assert reply.action.status == ActionStatus.CANCELLED

    def test_a_cancelled_reply_never_sends(self, tenancy, conversation, identity):
        reply = _due_now(_schedule(conversation, tenancy.user_for("agent")))
        services.cancel_scheduled_reply(reply)

        with registered(Platform.TELEGRAM) as adapter:
            _fire(reply)

        assert adapter.sends == []
        assert not _sent(conversation).exists()

    def test_a_sent_reply_cannot_be_rescheduled(self, tenancy, conversation, identity):
        reply = _due_now(_schedule(conversation, tenancy.user_for("agent")))
        with registered(Platform.TELEGRAM):
            _fire(reply)
        reply.refresh_from_db()

        with pytest.raises(services.InboxError):
            services.reschedule_reply(reply, body=BODY, send_at=timezone.now() + timedelta(hours=1))


class TestDismissingAFailure:
    def test_dismissing_takes_the_card_off_the_thread_and_keeps_the_reason(self, tenancy, conversation, identity):
        """`DISMISSED`, not `CANCELLED`: "I called it off" and "the platform
        refused it, and I have seen that" are different facts, and the second is
        the one an audit wants back."""
        from apps.inbox.selectors import failed_replies_for

        reply = _due_now(_schedule(conversation, tenancy.user_for("agent")))
        record_opt_out(identity, source="test")
        with registered(Platform.TELEGRAM):
            _fire(reply)
        reply.refresh_from_db()
        assert failed_replies_for(tenancy.workspace, conversation) == [reply]

        assert services.cancel_scheduled_reply(reply) is True

        reply.refresh_from_db()
        assert reply.status == DeferredStatus.DISMISSED
        assert reply.error
        assert failed_replies_for(tenancy.workspace, conversation) == []


class TestRearming:
    def test_editing_only_the_body_leaves_it_armed(self, tenancy, conversation, identity):
        """The regression this class exists for.

        ``schedule()`` returns an existing row *unchanged whatever its status*,
        so a re-arm key that can repeat hands back the row the reschedule just
        cancelled. Keying on the run time alone looks sufficient and misses
        exactly this: same time, new text, and the reply silently never sends.
        """
        when = timezone.now() + timedelta(hours=2)
        reply = _schedule(conversation, tenancy.user_for("agent"), send_at=when)
        first_action = reply.action

        services.reschedule_reply(reply, body=OutboundMessage(blocks=(TextBlock(text="v2"),)).to_body(), send_at=when)

        reply.refresh_from_db()
        assert reply.action_id != first_action.pk
        assert reply.action.status == ActionStatus.PENDING
        assert reply.will_fire is True

    def test_it_still_sends_once_after_that_edit(self, tenancy, conversation, identity):
        when = timezone.now() + timedelta(hours=2)
        reply = _schedule(conversation, tenancy.user_for("agent"), send_at=when)
        first_action = reply.action
        services.reschedule_reply(reply, body=BODY, send_at=when)
        reply.refresh_from_db()
        _due_now(reply)

        with registered(Platform.TELEGRAM) as adapter:
            handle_scheduled_reply({"scheduled_reply_id": str(reply.pk)}, first_action)
            handle_scheduled_reply({"scheduled_reply_id": str(reply.pk)}, reply.action)

        assert len(adapter.sends) == 1

    def test_the_arm_counter_is_what_the_key_carries(self, tenancy, conversation, identity):
        when = timezone.now() + timedelta(hours=2)
        reply = _schedule(conversation, tenancy.user_for("agent"), send_at=when)

        services.reschedule_reply(reply, body=BODY, send_at=when)

        reply.refresh_from_db()
        assert reply.arm_count == 2
        assert reply.action.idempotency_key.endswith(":2")


class TestDoubleSubmit:
    def test_the_same_compose_token_queues_one_reply(self, tenancy, conversation, identity):
        """A double-clicked "Schedule". Two rows would arm two queue actions with
        different keys, and the ``send_as_agent`` key is derived from the row —
        so nothing further down could collapse them either."""
        token = "b" * 32
        when = timezone.now() + timedelta(hours=2)

        first = services.schedule_reply(conversation, body=BODY, send_at=when, compose_token=token)
        second = services.schedule_reply(conversation, body=BODY, send_at=when, compose_token=token)

        assert first.pk == second.pk
        assert ScheduledReply.objects.for_workspace(tenancy.workspace).count() == 1

    def test_a_fresh_token_queues_a_second_reply(self, tenancy, conversation, identity):
        when = timezone.now() + timedelta(hours=2)

        services.schedule_reply(conversation, body=BODY, send_at=when, compose_token="c" * 32)
        services.schedule_reply(conversation, body=BODY, send_at=when, compose_token="d" * 32)

        assert ScheduledReply.objects.for_workspace(tenancy.workspace).count() == 2

    def test_no_token_means_no_guard(self, tenancy, conversation, identity):
        """A caller with no compose box — a future API, a flow action — must not
        be limited to one deferred row per workspace by a blank column."""
        when = timezone.now() + timedelta(hours=2)

        services.schedule_reply(conversation, body=BODY, send_at=when)
        services.schedule_reply(conversation, body=BODY, send_at=when)

        assert ScheduledReply.objects.for_workspace(tenancy.workspace).count() == 2

    def test_two_reminders_from_one_composer_render_both_stand(self, tenancy, conversation):
        """The reason reminders are *not* token-guarded: this endpoint does not
        refetch the compose box, so its token does not rotate — and a guard here
        would read a deliberate second reminder as a duplicate."""
        agent = tenancy.user_for("agent")

        services.schedule_reminder(conversation, recipient=agent, remind_at=timezone.now() + timedelta(hours=1))
        services.schedule_reminder(conversation, recipient=agent, remind_at=timezone.now() + timedelta(hours=3))

        from apps.inbox.models import InboxReminder

        assert InboxReminder.objects.for_workspace(tenancy.workspace).count() == 2


class TestASupersededAction:
    def test_a_claimed_action_cannot_send_at_the_old_time(self, tenancy, conversation, identity):
        """The reschedule race with teeth.

        ``_cancel_action`` can only cancel a PENDING queue row, so rescheduling
        after the worker has already claimed the original leaves that claimed
        action running with nothing to stop it — while the row is pending again,
        pointing at its replacement. The row-derived idempotency key is no help:
        it stops the *replacement* sending twice, which is the wrong one of the
        two to stop.
        """
        reply = _due_now(_schedule(conversation, tenancy.user_for("agent")))
        claimed = reply.action
        # The worker has it in hand; `_cancel_action` filters on PENDING and so
        # leaves it alone.
        ScheduledAction.objects.for_workspace(tenancy.workspace).filter(pk=claimed.pk).update(
            status=ActionStatus.RUNNING
        )
        services.reschedule_reply(reply, body=BODY, send_at=timezone.now() + timedelta(hours=5))
        reply.refresh_from_db()
        assert reply.action_id != claimed.pk

        with registered(Platform.TELEGRAM) as adapter:
            handle_scheduled_reply({"scheduled_reply_id": str(reply.pk)}, claimed)

        assert adapter.sends == []
        reply.refresh_from_db()
        assert reply.status == DeferredStatus.PENDING

    def test_the_replacement_still_sends_when_it_comes_due(self, tenancy, conversation, identity):
        reply = _due_now(_schedule(conversation, tenancy.user_for("agent")))
        claimed = reply.action
        ScheduledAction.objects.for_workspace(tenancy.workspace).filter(pk=claimed.pk).update(
            status=ActionStatus.RUNNING
        )
        services.reschedule_reply(reply, body=BODY, send_at=timezone.now() + timedelta(hours=5))
        reply.refresh_from_db()
        _due_now(reply)

        with registered(Platform.TELEGRAM) as adapter:
            handle_scheduled_reply({"scheduled_reply_id": str(reply.pk)}, claimed)
            handle_scheduled_reply({"scheduled_reply_id": str(reply.pk)}, reply.action)

        assert len(adapter.sends) == 1
