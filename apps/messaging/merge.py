"""Re-pointing messaging rows when two contacts are merged.

``apps.contacts.services.merge_contacts`` says it in its own docstring: "once
issue #8 lands, merging must *re-point* identities and conversations at the
survivor". Without this, the moment these tables exist a merge leaves a person's
identities and threads attached to a tombstone — the duplicate is
``status=deleted``, every read surface starts from active contacts, and the
conversation simply disappears from the inbox while its identity keeps receiving
webhooks.

The logic lives here rather than in ``apps/contacts`` so that app keeps knowing
nothing about messaging; ``merge_contacts`` reaches it through
``apps.flows.compat.installed_model``, the house pattern for a table that may
not be installed.
"""

import logging
from typing import Any

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.messaging.models import ContactChannelIdentity, Conversation, Message
from apps.queueing.models import ActionStatus, ActionType, ScheduledAction

logger = logging.getLogger(__name__)

__all__ = ["repoint_for_merge"]


def repoint_for_merge(primary: Any, duplicate: Any) -> None:
    """Move ``duplicate``'s identities and conversations onto ``primary``.

    Called from inside ``merge_contacts``' transaction, after it has checked
    that both contacts share a workspace.

    Identities re-point unconditionally: their unique keys are
    ``(connection, platform_user_id)`` and ``(workspace, platform,
    platform_user_id)``, and neither mentions the contact, so the move can never
    collide.

    Conversations can. ``(contact, connection)`` is unique, so if both people
    have a thread on the same connection the survivor's is kept and the
    duplicate's **messages** move into it — a merge must not lose message
    history, and it must not leave two threads for one person on one channel.
    """
    workspace_id = primary.workspace_id

    # workspace is not reassigned: merge_contacts has already refused a pair
    # that does not share one, so both rows already hold this value and writing
    # it again would imply to a reader that it can differ.
    identities = ContactChannelIdentity.objects.for_workspace(workspace_id).filter(contact=duplicate)
    moved_identities = identities.update(contact=primary)

    survivor_threads = {
        conversation.channel_connection_id: conversation
        for conversation in Conversation.objects.for_workspace(workspace_id).filter(contact=primary)
    }

    moved_threads = 0
    merged_threads = 0
    for conversation in Conversation.objects.for_workspace(workspace_id).filter(contact=duplicate):
        survivor = survivor_threads.get(conversation.channel_connection_id)
        if survivor is None:
            conversation.contact = primary
            conversation.save(update_fields=["contact", "updated_at"])
            moved_threads += 1
            continue

        _move_messages(workspace_id, conversation, survivor)
        _keep_latest(survivor, conversation)
        conversation.delete()
        merged_threads += 1

    retargeted = _retarget_send_retries(primary, duplicate)

    logger.info(
        "Merged messaging rows into contact %s: %s identities, %s threads moved, %s threads folded, "
        "%s retries retargeted",
        primary.pk,
        moved_identities,
        moved_threads,
        merged_threads,
        retargeted,
    )


def _retarget_send_retries(primary: Any, duplicate: Any) -> int:
    """Point the duplicate's pending ``send_retry`` actions at the survivor.

    ``ScheduledAction.contact_id`` is a plain ``UUIDField``, not a foreign key —
    deliberately, so the queue does not depend on the contacts app — which means
    nothing cascades it and a merge leaves it naming a tombstone. The worker
    takes ``contact_lock(action.contact_id)`` before running a handler, so the
    action would go on locking the *deleted* contact while its handler sent a
    message that now belongs to the survivor. The one-step-per-contact invariant
    (SPEC §9.6) would then be silently off: that send could interleave with flow
    work holding the survivor's lock, which is exactly what the lock exists to
    prevent.

    Only ``send_retry`` is retargeted, because it is the only action type this
    app owns. Other pending actions naming the duplicate belong to the streams
    that scheduled them (L3-B's ``resume_execution``, L6-A's ``sequence_step``),
    and reaching into those from here would be this app deciding how another's
    work should be locked.
    """
    return int(
        ScheduledAction.objects.for_workspace(primary.workspace_id)
        .filter(
            type=ActionType.SEND_RETRY,
            contact_id=duplicate.pk,
            status__in=(ActionStatus.PENDING, ActionStatus.RUNNING),
        )
        .update(contact_id=primary.pk, updated_at=timezone.now())
    )


def _move_messages(workspace_id: Any, absorbed: Conversation, survivor: Conversation) -> None:
    """Re-point ``absorbed``'s messages onto ``survivor``.

    Bypassing ``Message.save()`` is safe here and only here: both threads belong
    to the same workspace and the same connection, so the two columns that
    ``save()`` derives are already identical on every row.

    The bulk update can still fail. ``(conversation, idempotency_key)`` is
    unique, so if both threads hold a message with the same key — two sends that
    reused one across contacts, or one provider event persisted to both people
    before the merge — the move raises and takes the whole merge down with it.
    The fallback moves the rows one at a time and clears the key on the ones
    that collide: the key exists to stop a *future* insert being a duplicate
    send, and a message already delivered into a thread that is being merged
    away has no future insert to guard. The message itself is never dropped,
    because losing history is the one thing a merge must not do.
    """
    rows = Message.objects.for_workspace(workspace_id).filter(conversation=absorbed)
    try:
        with transaction.atomic():
            rows.update(conversation=survivor)
        return
    except IntegrityError:
        logger.info("Idempotency keys collide across the merged threads; moving messages individually.")

    for message in list(rows):
        message.conversation = survivor
        try:
            with transaction.atomic():
                message.save(update_fields=["conversation", "updated_at"])
        except IntegrityError:
            message.idempotency_key = ""
            with transaction.atomic():
                message.save(update_fields=["conversation", "idempotency_key", "updated_at"])


def _keep_latest(survivor: Conversation, absorbed: Conversation) -> None:
    """The surviving thread's recency is the later of the two.

    The inbox sorts on ``last_message_at``; a merge that lost the newer of the
    two timestamps would bury a live conversation.
    """
    stamps = [value for value in (survivor.last_message_at, absorbed.last_message_at) if value is not None]
    if stamps and survivor.last_message_at != max(stamps):
        survivor.last_message_at = max(stamps)
        survivor.save(update_fields=["last_message_at", "updated_at"])
