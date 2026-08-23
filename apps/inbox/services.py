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
from django.utils import timezone

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
