"""The inbox's own writes: the read cursor, labels, rules, and deferred work.

Every mutation of a *conversation* still goes through
:mod:`apps.messaging.services` (ROADMAP contract 1) or
:mod:`apps.contacts.services`. That is not a style preference: the agent-send
automation pause, compliance, idempotency and the send bucket all live inside the
facade, and ``apps/messaging/tests/test_write_sites.py`` is an AST scan that
fails the build if any module outside messaging assigns
``automation_paused_until``, ``window_expires_at`` or ``opted_out_at``.

What is here is the inbox's own tables. Two habits run through all of it.

*Schedule through the queue, cancel by pk.* ``apps.queueing`` has no cancel API —
``ActionStatus.CANCELLED`` is "set by owners of the work, never by the worker" —
so cancelling is an ``update()`` on the row this app stored the id of. Filtering
by ``type=`` instead would be a scan that could catch somebody else's action.

*An idempotency key carries the run time.* ``schedule()`` returns the existing row
**unchanged whatever its status** when a key is already present, so a key without
the time in it would make "move this reminder from 3pm to 5pm" hand back the 3pm
row and 5pm would never fire. ``apps/messaging/handlers.py`` learned this first
and its comment is the longer version.
"""

from datetime import datetime
from typing import Any

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.inbox.models import (
    DEFAULT_LABEL_COLOR,
    MAX_LABELS_PER_CONVERSATION,
    ConversationLabel,
    ConversationLabelLink,
    ConversationRead,
    DeferredStatus,
    InboxReminder,
    InboxRule,
    ScheduledReply,
)
from apps.messaging.models import Conversation

__all__ = [
    "PRIORITY_STEP",
    "REMINDER",
    "SCHEDULED_REPLY",
    "InboxError",
    "apply_label",
    "cancel_reminder",
    "cancel_scheduled_reply",
    "create_label",
    "mark_read",
    "remove_label",
    "reorder_rules",
    "reschedule_reply",
    "schedule_reminder",
    "schedule_reply",
    "update_label",
]

#: The queue action types this app registers. Local constants rather than new
#: ``ActionType`` members: that enum is documented as "not a closed set", the
#: handler registry is the authority, and ``apps/flows/triggers/handlers.py``
#: set the precedent with ``ROUTE_EVENT``. Adding members there would be an edit
#: to a lower layer for no behavioural gain.
REMINDER = "reminder"
SCHEDULED_REPLY = "scheduled_reply"

#: Rules are renormalised to 0, 10, 20… on every reorder, so there is always room
#: to drop one between two others. The same convention, and the same number,
#: ``apps.flows.triggers.services`` uses for trigger priority.
PRIORITY_STEP = 10


class InboxError(ValueError):
    """Something the inbox refuses to do, phrased for an operator."""


def mark_read(conversation: Conversation, user: Any, *, at: datetime) -> ConversationRead:
    """Move this member's cursor to ``at``, never backwards.

    Monotonic on purpose. Two tabs polling the same thread land here out of
    order often enough to matter, and a cursor that can move backwards makes an
    already-read conversation reappear as unread — the badge flickering with
    nothing having happened is worse than a badge that is occasionally a beat
    behind.

    The comparison happens **in the database**, as a condition on the UPDATE.
    Reading the row and then deciding in Python is not monotonic however tight
    the transaction is: two overlapping requests both read the same stored
    value, both conclude they are newer, and whichever commits last wins — so
    the one carrying the *earlier* timestamp can drag the cursor backwards,
    which is the thing this function exists to prevent. ``last_read_at__lt=at``
    lets the row itself arbitrate, and the update simply matches nothing when a
    newer cursor is already there.
    """
    rows = ConversationRead.objects.for_workspace(conversation.workspace_id)
    with transaction.atomic():
        row, created = rows.get_or_create(conversation=conversation, user=user, defaults={"last_read_at": at})
        if not created and rows.filter(pk=row.pk, last_read_at__lt=at).update(
            last_read_at=at, updated_at=timezone.now()
        ):
            # Only when the UPDATE actually matched, so the in-memory row agrees
            # with what is stored rather than with what we asked for.
            row.last_read_at = at
    return row


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------


def create_label(workspace: Any, *, name: str, color: str = "") -> ConversationLabel:
    """Add a label to the workspace's palette.

    ``full_clean()`` rather than a bare ``save()``: the uniqueness rule is a
    ``Lower("name")`` expression constraint and Django validates those in
    ``full_clean``, so this is what turns a duplicate into a sentence the form
    can render instead of an ``IntegrityError`` five frames later. It also runs
    the colour validator, which is the write half of the two-sided check
    :mod:`apps.inbox.rendering` completes.
    """
    label = ConversationLabel(workspace=workspace, name=(name or "").strip(), color=_color(color))
    _validated(label, fallback="That label is not valid.")
    label.save()
    return label


def update_label(label: ConversationLabel, *, name: str, color: str) -> ConversationLabel:
    """Rename or recolour a label.

    Both are in the conversation list's ETag — it hashes the chips a row prints,
    not their ids — so a rename really does reach every open tab on the next
    poll rather than on the next message.
    """
    label.name = (name or "").strip()
    label.color = _color(color)
    _validated(label, fallback="That label is not valid.")
    label.save(update_fields=["name", "color", "updated_at"])
    return label


def apply_label(conversation: Conversation, label: ConversationLabel, *, by: Any = None) -> bool:
    """Put a label on a thread. False when it was already there.

    Idempotent through the unique constraint rather than a read first: the rules
    hook, a double-clicking operator and a bulk action all land here, and a
    select-then-insert would only widen the window the constraint closes.
    """
    links = ConversationLabelLink.objects.for_workspace(conversation.workspace_id)
    if links.filter(conversation=conversation).count() >= MAX_LABELS_PER_CONVERSATION:
        raise InboxError(f"A conversation can carry at most {MAX_LABELS_PER_CONVERSATION} labels.")
    try:
        with transaction.atomic():
            ConversationLabelLink(conversation=conversation, label=label, applied_by=by).save()
    except IntegrityError:
        return False
    return True


def remove_label(conversation: Conversation, label: ConversationLabel) -> bool:
    """Take a label off a thread. False when it was not there.

    A hard delete, unlike the deferred-work rows below: a link carries no outcome
    worth keeping, and the list's ETag is built from the chips a row *renders*
    rather than from an aggregate over this table, so a removal is visible to a
    poller without the row having to survive.
    """
    removed, _ = (
        ConversationLabelLink.objects.for_workspace(conversation.workspace_id)
        .filter(conversation=conversation, label=label)
        .delete()
    )
    return bool(removed)


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


def reorder_rules(workspace: Any, ordered_ids: list[str]) -> int:
    """Renormalise this workspace's rules to 0, 10, 20… in the order given.

    Ids the workspace does not own are ignored rather than refused: the drag
    handle posts whatever the DOM held, and a rule deleted in another tab would
    otherwise turn an ordinary reorder into an error the operator cannot act on.
    Rules the list did not mention keep their relative order and follow.

    ``select_for_update`` because two operators dragging at once would otherwise
    interleave their renumbering — the same reason
    ``apps.flows.triggers.services.move_trigger`` takes it.
    """
    wanted = [str(value) for value in ordered_ids if value]
    with transaction.atomic():
        rows = list(InboxRule.objects.for_workspace(workspace).select_for_update().order_by("priority", "name"))
        by_id = {str(row.pk): row for row in rows}
        ordered = [by_id[value] for value in wanted if value in by_id]
        ordered += [row for row in rows if str(row.pk) not in set(wanted)]

        moved = []
        now = timezone.now()
        for index, row in enumerate(ordered):
            priority = index * PRIORITY_STEP
            if row.priority != priority:
                row.priority = priority
                # Stamped by hand. ``updated_at`` is ``auto_now``, which Django
                # applies in ``Model.save()`` and **not** in ``bulk_update`` — so
                # listing the field without setting it writes the stale value
                # back and reads, wrongly, as "and bump the timestamp".
                row.updated_at = now
                moved.append(row)
        if moved:
            # Scoped explicitly: bulk_update is not one of the terminals the
            # enforcing manager guards, so an unscoped `.objects.bulk_update`
            # would run without complaint. These rows were read scoped a moment
            # ago; spelling it again keeps the write greppable next to the read.
            InboxRule.objects.for_workspace(workspace).bulk_update(moved, ["priority", "updated_at"])
    return len(moved)


# ---------------------------------------------------------------------------
# Reminders
# ---------------------------------------------------------------------------


def schedule_reminder(
    conversation: Conversation,
    *,
    recipient: Any,
    remind_at: datetime,
    note: str = "",
    created_by: Any = None,
    compose_token: str = "",
) -> InboxReminder:
    """Arrange an in-app nudge about this thread (SPEC §14)."""
    if remind_at <= timezone.now():
        raise InboxError("Pick a time in the future.")
    existing = _already_arranged(InboxReminder, conversation, compose_token)
    if existing is not None:
        return existing
    reminder = InboxReminder(
        conversation=conversation,
        recipient=recipient,
        remind_at=remind_at,
        note=(note or "").strip(),
        created_by=created_by,
        compose_token=compose_token,
    )
    _validated(reminder, fallback="That reminder is not valid.")
    if not _saved(reminder):
        # Two genuinely simultaneous posts both missed the read above; the
        # constraint arbitrated, and the loser reads the winner's row back.
        return _already_arranged(InboxReminder, conversation, compose_token) or reminder
    _arm(reminder, REMINDER, reminder.remind_at, {"reminder_id": str(reminder.pk)})
    return reminder


def cancel_reminder(reminder: InboxReminder) -> bool:
    """Call it off. False when it had already fired or been cancelled."""
    return _stand_down(reminder)


# ---------------------------------------------------------------------------
# Scheduled replies
# ---------------------------------------------------------------------------


def schedule_reply(
    conversation: Conversation,
    *,
    body: dict[str, Any],
    send_at: datetime,
    created_by: Any = None,
    compose_token: str = "",
) -> ScheduledReply:
    """Queue a reply to go out later (SPEC §14).

    Compliance is **not** consulted here. A window can close between now and
    then, so the decision that counts is the one ``send_as_agent`` makes when the
    action fires; asking now would only produce a promise this function cannot
    keep. The composer still shows the current verdict, as a courtesy rather than
    a gate.
    """
    if send_at <= timezone.now():
        raise InboxError("Pick a time in the future.")
    existing = _already_arranged(ScheduledReply, conversation, compose_token)
    if existing is not None:
        return existing
    reply = ScheduledReply(
        conversation=conversation,
        body=body,
        send_at=send_at,
        created_by=created_by,
        compose_token=compose_token,
    )
    if not _saved(reply):
        return _already_arranged(ScheduledReply, conversation, compose_token) or reply
    _arm(reply, SCHEDULED_REPLY, reply.send_at, {"scheduled_reply_id": str(reply.pk)})
    return reply


def reschedule_reply(reply: ScheduledReply, *, body: dict[str, Any], send_at: datetime) -> ScheduledReply:
    """Edit a pending scheduled reply.

    Cancels the old queue row and arms a new one rather than moving ``run_at`` on
    the existing one. Two reasons, and the second is the load-bearing one: the
    worker may already have claimed the row, and ``schedule()``'s key carries the
    time, so a new time is a new key by construction — mutating in place would
    leave the key describing a moment that had passed.
    """
    if reply.status != DeferredStatus.PENDING:
        raise InboxError("That reply has already been sent or cancelled.")
    if send_at <= timezone.now():
        raise InboxError("Pick a time in the future.")
    _cancel_action(reply)
    reply.body = body
    reply.send_at = send_at
    reply.save(update_fields=["body", "send_at", "updated_at"])
    _arm(reply, SCHEDULED_REPLY, reply.send_at, {"scheduled_reply_id": str(reply.pk)})
    return reply


def cancel_scheduled_reply(reply: ScheduledReply) -> bool:
    """Take a scheduled reply off the thread. False when there was nothing to do.

    Two meanings behind one button, because the operator's intent is the same
    either way — "I am done with this" — while the record must not be:

    * ``PENDING`` → ``CANCELLED``, and the queue row is cancelled with it.
    * ``FAILED`` → ``DISMISSED``. The ``error`` column stays, so why it never
      went out survives being acknowledged; only the card leaves the thread.

    A sent one is not dismissable: it is in the history like any other message.
    """
    if reply.status == DeferredStatus.FAILED:
        reply.status = DeferredStatus.DISMISSED
        reply.save(update_fields=["status", "updated_at"])
        return True
    return _stand_down(reply)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _saved(row: Any) -> bool:
    """Insert the row. False when the compose-token constraint refused it.

    In its own ``atomic()`` block: catching an ``IntegrityError`` without a
    savepoint poisons the surrounding transaction, and these services are called
    from views that may already be inside one.
    """
    try:
        with transaction.atomic():
            row.save()
    except IntegrityError:
        return False
    return True


def _already_arranged(model: Any, conversation: Conversation, compose_token: str) -> Any:
    """The row this compose token already produced, if it did (SPEC §9.4).

    The deferred half of the idempotency the live send path gets from
    ``message_unique_conv_idem``: the compose box mints one token per render, so
    a double-clicked "Schedule" arrives twice carrying the same one and must
    become one queued reply rather than two messages to the contact. Without
    this the two rows have different pks, arm different queue keys, and the
    ``send_as_agent`` key — which is derived from the row — cannot collapse them
    either.

    A read, with the partial unique constraint behind it as the real arbiter: two
    genuinely simultaneous posts both miss here, and the second one's
    ``IntegrityError`` is caught by the caller's ``_validated``/save path.
    """
    if not compose_token:
        return None
    return model.objects.for_workspace(conversation.workspace_id).filter(compose_token=compose_token).first()


def _arm(row: Any, action_type: str, run_at: datetime, payload: dict[str, Any]) -> None:
    """Put this row's work on the queue and remember which row it is.

    The contact travels with it, so the worker takes that contact's advisory lock
    before running the handler (SPEC §9.6) and a scheduled reply cannot interleave
    with a flow step for the same person. It is also what makes
    ``apps.contacts.activity.stand_down`` cancel this action when the contact is
    deleted, without that module needing to know these tables exist.

    **The key carries ``arm_count``, not the run time.** ``schedule()`` returns an
    existing row *unchanged whatever its status*, so a key that can repeat is a
    key that can hand back a row this function's caller just cancelled. Keying on
    ``(pk, run_at)`` looks sufficient — a later time is a later key — and misses
    the case that matters: editing a scheduled reply's text without touching its
    time re-mints the identical key, gets the cancelled action back, and the
    reply silently never sends. A counter is monotonic by construction, which is
    the property actually needed.
    """
    from apps.queueing.registry import schedule

    row.arm_count += 1
    action = schedule(
        action_type,
        run_at,
        payload,
        # The Workspace instance, not its id: schedule() assigns straight to the
        # FK and an id there raises.
        workspace=row.conversation.workspace,
        contact=row.conversation.contact_id,
        idempotency_key=f"inbox:{action_type}:{row.pk}:{row.arm_count}",
    )
    row.action = action
    row.save(update_fields=["action", "arm_count", "updated_at"])


def _stand_down(row: Any) -> bool:
    """Cancel a pending row and the queue work behind it."""
    if row.status != DeferredStatus.PENDING:
        return False
    _cancel_action(row)
    row.status = DeferredStatus.CANCELLED
    row.save(update_fields=["status", "updated_at"])
    return True


def _cancel_action(row: Any) -> None:
    """Stop the queue row this one armed, by pk.

    By pk rather than by ``type=`` and ``contact=``: a filter on the type would
    also catch a *different* reminder for the same contact, and there is no cancel
    API to do it for us — ``ActionStatus.CANCELLED`` is documented as set by the
    owner of the work. ``status=PENDING`` in the filter means a row the worker has
    already claimed is left alone; the handler's own ``status != PENDING`` guard
    is what makes that safe.
    """
    from apps.queueing.models import ActionStatus, ScheduledAction

    if row.action_id is None:
        return
    ScheduledAction.objects.for_workspace(row.workspace_id).filter(
        pk=row.action_id, status=ActionStatus.PENDING
    ).update(status=ActionStatus.CANCELLED, updated_at=timezone.now())


def _validated(instance: Any, *, fallback: str) -> None:
    try:
        instance.full_clean()
    except ValidationError as exc:
        raise InboxError(_first_message(exc, fallback=fallback)) from exc


def _first_message(exc: ValidationError, *, fallback: str) -> str:
    """One sentence out of a ``ValidationError``, for a toast.

    Field-agnostic: the caller has no form to hang per-field errors on, and the
    first message is always the one the operator can act on.
    """
    for messages in getattr(exc, "message_dict", {}).values():
        for message in messages:
            return str(message)
    for message in getattr(exc, "messages", []):
        return str(message)
    return fallback


def _color(value: str) -> str:
    return (value or "").strip() or DEFAULT_LABEL_COLOR
