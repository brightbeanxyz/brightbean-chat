"""The worker half of the inbox: a reminder comes due, a scheduled reply goes out.

Registered types, not new enum members. ``apps.queueing.models.ActionType`` is
documented as **not a closed set** — "the authority on what may be processed is
the handler registry" — and it is deliberately not attached to the column as
``choices=`` so that a later app can register a type without shipping an
``AlterField`` migration in ``apps.queueing``. Its docstring names these two by
hand. ``apps/flows/triggers/handlers.py`` set the precedent with a local
``ROUTE_EVENT`` constant, and :mod:`apps.inbox.services` holds ours.

Three things the queue guarantees, so nothing here re-does them: the handler runs
inside a transaction that already holds the contact advisory lock (both rows name
a contact); raising retries on SPEC §15's backoff ladder; returning normally
marks the row done in the same transaction as the work.

Which makes the interesting decision here the *opposite* of raising, twice over.

**Nothing raises after the send.** ``send_as_agent``'s message row and its
``dispatched_at`` claim are savepoints inside this handler's transaction, so a
raise after the provider call has gone out rolls both back while the message is
already delivered — and the retry sends it again. Everything fallible therefore
happens **before** the send; after it there are only status writes, and even the
notification is wrapped.

**A refused send is not a retry.** Compliance denying at fire time is the
scheduled reply's answer, not a transient failure: five attempts over six hours
cannot reopen a messaging window. It is recorded, surfaced and notified, and the
handler returns — the shape ``apps/messaging/handlers.py::handle_send_retry``
already uses for the same verdict.
"""

import logging
from typing import Any

from apps.inbox.notifications import EVENT_REMINDER, EVENT_SCHEDULED_REPLY_FAILED
from apps.inbox.services import REMINDER, SCHEDULED_REPLY
from apps.queueing.models import ScheduledAction
from apps.queueing.registry import register_handler

__all__ = ["handle_reminder", "handle_scheduled_reply"]

logger = logging.getLogger(__name__)


@register_handler(REMINDER, replace=True)
def handle_reminder(payload: dict[str, Any], action: ScheduledAction) -> None:
    """Turn a due reminder into an in-app notification with a deep link.

    ``replace=True`` because ``ready()`` runs twice under some autoreload paths —
    the same reason ``apps/messaging/handlers.py`` gives.
    """
    from apps.inbox.models import DeferredStatus, InboxReminder

    reminder = _load(InboxReminder, payload, action, "reminder_id")
    if reminder is None or reminder.status != DeferredStatus.PENDING:
        # Cancelled between the enqueue and the claim, or already fired and
        # re-run by zombie recovery. Both are ordinary, and both mean there is
        # nothing left to do.
        return

    _notify(
        reminder.workspace,
        EVENT_REMINDER,
        recipient=reminder.recipient,
        conversation=reminder.conversation,
        context={
            "contact_name": reminder.conversation.contact.display_name,
            "note": reminder.note,
        },
    )
    reminder.status = DeferredStatus.SENT
    reminder.save(update_fields=["status", "updated_at"])


@register_handler(SCHEDULED_REPLY, replace=True)
def handle_scheduled_reply(payload: dict[str, Any], action: ScheduledAction) -> None:
    """Send a reply an agent composed earlier, as an agent.

    Through ``send_as_agent`` and nowhere else, which buys three things this
    module then does not implement: compliance re-decided at *this* moment,
    ``automation_paused_until`` extended because a scheduled reply is still an
    agent taking over, and the idempotency key deduplicating the provider call.
    """
    from apps.inbox.models import DeferredStatus, ScheduledReply
    from apps.messaging.models import MessageStatus
    from apps.messaging.rendering import outbound_from_body
    from apps.messaging.services import send_as_agent

    reply = _load(ScheduledReply, payload, action, "scheduled_reply_id")
    if reply is None or reply.status != DeferredStatus.PENDING:
        return

    conversation = reply.conversation
    outbound = outbound_from_body(reply.body)
    if not outbound.blocks:
        # An empty body cannot become a message, and no number of retries will
        # give it one. Recorded as a failure rather than swallowed, because a
        # reply an agent believed was queued has to end somewhere visible.
        _fail(reply, "empty_body", "That scheduled reply had nothing to send.")
        return

    # ---- nothing below this line may raise before the send returns ----
    message = send_as_agent(
        workspace=conversation.workspace,
        contact=conversation.contact,
        connection=conversation.channel_connection,
        outbound=outbound,
        # Derived from this row and nothing else — not the action, not the
        # attempt, not the time. A reschedule mints a new action for the same
        # logical send, and a re-run after zombie recovery is the same send
        # again; both have to collapse onto one Message through
        # ``message_unique_conv_idem`` rather than deliver twice.
        idempotency_key=f"inbox:scheduled:{reply.pk}",
    )

    if message.status == MessageStatus.FAILED:
        from apps.messaging.codes import describe

        _fail(reply, message.error, describe(message.error), message=message)
        return

    reply.status = DeferredStatus.SENT
    reply.message = message
    reply.save(update_fields=["status", "message", "updated_at"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fail(reply: Any, code: str, reason: str, *, message: Any = None) -> None:
    """Record a scheduled reply that did not go out, and say so out loud.

    "Never a silent drop" is the acceptance criterion, and it has two halves: the
    row and the thread show the failure, and the person who queued it is told.
    The notification is best-effort — a notification backend having a bad day
    must not roll back the record of *why* the send failed, which is the more
    important of the two.
    """
    from apps.inbox.models import DeferredStatus

    reply.status = DeferredStatus.FAILED
    # The machine-readable code, never a provider sentence: those quote the
    # request that caused them, credentials included (SECURITY-BASELINE §5).
    reply.error = (code or "")[:200]
    if message is not None:
        reply.message = message
    reply.save(update_fields=["status", "error", "message", "updated_at"])

    conversation = reply.conversation
    _notify(
        reply.workspace,
        EVENT_SCHEDULED_REPLY_FAILED,
        recipient=reply.created_by,
        conversation=conversation,
        context={"contact_name": conversation.contact.display_name, "reason": reason},
    )


def _notify(workspace: Any, event: str, *, recipient: Any, conversation: Any, context: dict[str, Any]) -> None:
    """Send one in-app notification, with the thread as its deep link.

    ``users=[]`` would mean *nobody* — the notification engine distinguishes it
    from ``None`` deliberately — so a recipient who has left the workspace falls
    back to its admins rather than to silence. A reminder nobody receives is the
    same bug as a reminder that never fired.

    Wrapped, and the reason is the module docstring's: on the scheduled-reply
    path this runs after the provider call, where an exception would roll the
    message row back under a message that has already been delivered.
    """
    from django.urls import reverse

    from apps.notifications.engine import notify

    payload = dict(context)
    payload["action_url"] = reverse(
        "inbox:thread",
        kwargs={"workspace_id": conversation.workspace_id, "conversation_id": conversation.pk},
    )
    users = [recipient] if _still_a_member(workspace, recipient) else None
    try:
        notify(workspace, event, users=users, roles=None if users else ("admin",), context=payload)
    except Exception:
        logger.exception("Could not notify %s about %s", getattr(recipient, "pk", None), event)


def _still_a_member(workspace: Any, user: Any) -> bool:
    from apps.members.models import WorkspaceMembership

    if user is None:
        return False
    return WorkspaceMembership.objects.filter(workspace=workspace, user=user).exists()


def _load(model: Any, payload: dict[str, Any], action: ScheduledAction, key: str) -> Any:
    """The row this action names, scoped to the action's own workspace.

    A payload is a document that has been sitting in a table, possibly for hours,
    so its ids are ids and not trusted objects — the discipline
    ``apps/flows/handlers.py`` spells out. ``select_related`` because every caller
    reaches straight through to the conversation, its contact and its connection.
    """
    row_id = payload.get(key)
    if not row_id or action.workspace_id is None:
        return None
    return (
        model.objects.for_workspace(action.workspace_id)
        .filter(pk=row_id)
        .select_related("conversation", "conversation__contact", "conversation__channel_connection")
        .first()
    )
