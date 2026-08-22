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

from apps.messaging.models import ContactChannelIdentity, Conversation, Message

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

    identities = ContactChannelIdentity.objects.for_workspace(workspace_id).filter(contact=duplicate)
    moved_identities = identities.update(contact=primary, workspace=workspace_id)

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

        # Bypassing Message.save() is safe here and only here: both threads
        # belong to the same workspace and the same connection, so the two
        # columns that save() derives are already identical on every row.
        Message.objects.for_workspace(workspace_id).filter(conversation=conversation).update(conversation=survivor)
        _keep_latest(survivor, conversation)
        conversation.delete()
        merged_threads += 1

    logger.info(
        "Merged messaging rows into contact %s: %s identities, %s threads moved, %s threads folded",
        primary.pk,
        moved_identities,
        moved_threads,
        merged_threads,
    )


def _keep_latest(survivor: Conversation, absorbed: Conversation) -> None:
    """The surviving thread's recency is the later of the two.

    The inbox sorts on ``last_message_at``; a merge that lost the newer of the
    two timestamps would bury a live conversation.
    """
    stamps = [value for value in (survivor.last_message_at, absorbed.last_message_at) if value is not None]
    if stamps and survivor.last_message_at != max(stamps):
        survivor.last_message_at = max(stamps)
        survivor.save(update_fields=["last_message_at", "updated_at"])
