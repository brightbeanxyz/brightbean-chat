"""The inbox's own state: one read cursor per member, per conversation.

Everything else the inbox shows lives in ``apps.messaging`` and is read-only
here — ROADMAP contract 1 makes ``messaging/services.py`` the only way to mutate
a conversation or a message, and contract 3 makes ``automation_paused_until``
messaging's to write. What messaging does *not* have is any notion of who has
read what: SPEC §14 asks the conversation list for "unread indication via a
per-member read cursor", and there is no cursor, no unread flag and no activity
model anywhere in that app.

So the cursor lives here, in the app that needs it. It is the inbox's own
bookkeeping about its own reader rather than tenant-visible conversation state,
which is why adding it does not cross the facade boundary.
"""

from typing import Any

from django.conf import settings
from django.db import models

from apps.common.scoping import WorkspaceScopedModel

__all__ = ["ConversationRead"]


class ConversationRead(WorkspaceScopedModel):
    """How far one member has read into one conversation.

    "Unread" is deliberately **not** ``conversation.last_message_at >
    last_read_at``. ``last_message_at`` moves for outbound sends and internal
    notes too, so that definition would light the badge for the agent's own
    reply. :func:`apps.inbox.selectors.with_unread` asks the question the
    operator actually means: is there an *inbound* message newer than my cursor.
    """

    conversation = models.ForeignKey(
        "messaging.Conversation",
        on_delete=models.CASCADE,
        related_name="reads",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="conversation_reads",
    )
    last_read_at = models.DateTimeField()

    class Meta:
        db_table = "inbox_conversation_read"
        constraints = [
            models.UniqueConstraint(fields=["conversation", "user"], name="read_unique_conv_user"),
        ]
        indexes = [
            # The sidebar badge and the list annotation both start from
            # (workspace, user) and join out to conversations from there.
            models.Index(fields=["workspace", "user"], name="read_ws_user_idx"),
        ]

    def __str__(self) -> str:
        return f"read {str(self.conversation_id)[:8]} by {self.user_id}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Derive ``workspace`` from the conversation.

        The same discipline — and the same ``update_fields`` caveat — as
        :meth:`apps.messaging.models.Message.save`: Django reads a *falsy*
        ``update_fields`` as "save nothing" and returns before touching the
        database, so widening an empty one would turn a documented no-op into a
        real UPDATE.
        """
        self.workspace_id = self.conversation.workspace_id
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            widened = set(update_fields)
            kwargs["update_fields"] = widened | {"workspace"} if widened else widened
        super().save(*args, **kwargs)
