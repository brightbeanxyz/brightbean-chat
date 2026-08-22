"""The inbox's own writes — which is the read cursor, and nothing else.

Every other mutation this app performs goes through
:mod:`apps.messaging.services` (ROADMAP contract 1) or
:mod:`apps.contacts.services`. That is not a style preference: the agent-send
automation pause, compliance, idempotency and the send bucket all live inside
the facade, and ``apps/messaging/tests/test_write_sites.py`` is an AST scan that
fails the build if any module outside messaging assigns
``automation_paused_until``, ``window_expires_at`` or ``opted_out_at``.
"""

from datetime import datetime
from typing import Any

from django.db import transaction

from apps.inbox.models import ConversationRead
from apps.messaging.models import Conversation

__all__ = ["mark_read"]


def mark_read(conversation: Conversation, user: Any, *, at: datetime) -> ConversationRead:
    """Move this member's cursor to ``at``, never backwards.

    Monotonic on purpose. Two tabs polling the same thread land here out of
    order often enough to matter, and a cursor that can move backwards makes an
    already-read conversation reappear as unread — the badge flickering with
    nothing having happened is worse than a badge that is occasionally a beat
    behind.

    ``get_or_create`` inside a transaction rather than ``update_or_create``,
    because the comparison has to happen against the row as stored.
    """
    with transaction.atomic():
        row, created = ConversationRead.objects.for_workspace(conversation.workspace_id).get_or_create(
            conversation=conversation,
            user=user,
            defaults={"last_read_at": at},
        )
        if not created and row.last_read_at < at:
            row.last_read_at = at
            row.save(update_fields=["last_read_at", "updated_at"])
    return row
