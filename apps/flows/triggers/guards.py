"""The two durable guards SPEC §10 asks for: the 24-hour default reply, and comments.

Both are "may I do this once" questions answered by the database rather than by
a read followed by a write, because both are asked concurrently. Two events from
one contact arrive in a single delivery and
:func:`apps.messaging.ingest.persist_events` gives each its own transaction, so
a ``SELECT`` then ``INSERT`` lets both through — every time, not rarely.
"""

import logging
from datetime import datetime, timedelta
from typing import Any

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.flows.models import DefaultReplyState, HandledComment

__all__ = [
    "DEFAULT_REPLY_INTERVAL",
    "PRIVATE_REPLY_WINDOW",
    "claim_default_reply",
    "may_private_reply",
    "mark_private_reply_sent",
    "private_reply_deadline",
    "record_comment",
]

logger = logging.getLogger(__name__)

#: SPEC §10: "frequency guard fixed 24h". Fixed as in not configurable, and
#: rolling as in measured from the last actual send — see below.
DEFAULT_REPLY_INTERVAL = timedelta(hours=24)

#: SPEC §10: a comment's private reply has seven days from the comment.
PRIVATE_REPLY_WINDOW = timedelta(days=7)

#: Platform ids are attacker-controlled. Bounded on write to the column width.
_MAX_PLATFORM_ID = 200


def claim_default_reply(contact: Any, connection: Any, *, now: datetime | None = None) -> bool:
    """May a default reply go out now — and if so, take the guard.

    Rolling, not clock-aligned: the window restarts from the last send, so there
    is no boundary at which two replies seconds apart are both allowed. That is
    the whole reason this is not :func:`apps.common.ratelimit.hit`.

    Correct **without** the contact advisory lock, which matters because the
    caller's hold on it is a property of one code path rather than of this
    function: the ``UPDATE`` takes a row lock for its own duration, so of two
    concurrent claims exactly one sees ``updated == 1``, and the very first claim
    for a pair is arbitrated by the unique constraint instead.
    """
    moment = now or timezone.now()
    updated = (
        DefaultReplyState.objects.for_workspace(contact.workspace_id)
        .filter(
            contact=contact,
            channel_connection=connection,
            last_sent_at__lte=moment - DEFAULT_REPLY_INTERVAL,
        )
        .update(last_sent_at=moment, updated_at=moment)
    )
    if updated:
        return True

    try:
        # Its own atomic block: an IntegrityError caught without one poisons the
        # caller's transaction, and the caller here is holding the contact lock.
        with transaction.atomic():
            DefaultReplyState(
                workspace_id=contact.workspace_id,
                contact=contact,
                channel_connection=connection,
                last_sent_at=moment,
            ).save()
        return True
    except IntegrityError:
        # A row exists and is inside the window, or a concurrent claim won the
        # insert. Both mean the same thing to the caller.
        return False


def record_comment(
    *,
    connection: Any,
    trigger: Any,
    comment_id: str,
    post_id: str,
    commenter_ref: str,
    commented_at: datetime,
    once_per_contact_per_post: bool = True,
    now: datetime | None = None,
) -> HandledComment | None:
    """Claim this comment, or ``None`` when somebody already holds the guard.

    **The IntegrityError is the guard.** Both constraints on
    :class:`apps.flows.models.HandledComment` are checked by this one insert: the
    redelivery guard on ``(connection, comment_id)`` and, when the trigger asks
    for it, once-per-commenter-per-post. A check-then-insert would pass both
    halves of a race and send two private replies.
    """
    moment = now or timezone.now()
    try:
        with transaction.atomic():
            row = HandledComment(
                workspace_id=connection.workspace_id,
                channel_connection=connection,
                trigger=trigger,
                comment_id=comment_id[:_MAX_PLATFORM_ID],
                post_id=post_id[:_MAX_PLATFORM_ID],
                commenter_ref=commenter_ref[:_MAX_PLATFORM_ID],
                # The clock is ours, not the platform's — the rule
                # apps/messaging/ingest.py already applies to inbound
                # timestamps. A comment dated next week would otherwise buy
                # itself extra days inside the seven-day reply window.
                commented_at=min(commented_at, moment),
                once_per_contact_per_post=once_per_contact_per_post,
            )
            row.save()
            return row
    except IntegrityError:
        return None


def private_reply_deadline(row: HandledComment) -> datetime:
    """When the platform stops accepting a private reply to this comment."""
    return row.commented_at + PRIVATE_REPLY_WINDOW


def may_private_reply(row: HandledComment, *, now: datetime | None = None) -> bool:
    """Whether a private reply is still allowed and has not already been sent."""
    if row.private_reply_sent_at is not None:
        return False
    return (now or timezone.now()) <= private_reply_deadline(row)


def mark_private_reply_sent(row: HandledComment, *, contact: Any = None, now: datetime | None = None) -> None:
    """Record that the flow's first message went out, and to whom.

    The contact arrives here rather than at insert time because
    ``apps/messaging/ingest.py`` creates none for a comment event — the identity
    exists only once the private reply opens a DM thread.
    """
    row.private_reply_sent_at = now or timezone.now()
    fields = ["private_reply_sent_at", "updated_at"]
    if contact is not None:
        row.contact = contact
        fields.append("contact")
    row.save(update_fields=fields)
