"""Queries against a person's notification feed.

One module so the "scope by user" rule is written once rather than repeated at
six call sites. Every view, the context processor and the admin go through
here; a query that forgets ``user=`` is then a missing import rather than a
data leak.

None of these are workspace-scoped queries. ``Notification`` is a plain
``BaseModel`` — see the ``models`` docstring for why — so ``objects`` is
Django's ordinary manager and there is no ``UnscopedQueryError`` to satisfy. The
tenancy boundary for this table is the person, and it is enforced here.
"""

from typing import Any

from apps.notifications.models import Notification

__all__ = ["feed_for", "recent_for", "unread_count_for"]

#: How many rows the bell shows before "view all" takes over.
BELL_LIMIT = 20


def feed_for(user: Any) -> Any:
    """Every notification addressed to ``user``, newest first."""
    return Notification.objects.filter(user=user)


def recent_for(user: Any, limit: int = BELL_LIMIT) -> list[Notification]:
    """The bell's window."""
    return list(feed_for(user)[:limit])


def unread_count_for(user: Any) -> int:
    """The badge. Served by the (user, is_read, -created_at) index."""
    return feed_for(user).filter(is_read=False).count()
