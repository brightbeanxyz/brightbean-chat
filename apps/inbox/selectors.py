"""Every read the inbox performs. No writes live here.

Two things in this module are load-bearing and easy to get subtly wrong:

**Unread.** ``Conversation.last_message_at`` moves for *any* message — the
agent's own reply and an internal note included — so "last_message_at is newer
than my cursor" would light the badge the instant an agent answered. The
question SPEC §14 is actually asking is whether the *contact* has said something
since this member last looked, so :func:`with_unread` tests for an inbound
message newer than the cursor and nothing else. That is also what the issue
means by keeping notes out of the contact-visible counts.

**The version tokens.** The pollers need a value that changes when, and only
when, the rendered markup would, and the two surfaces reach that differently.

:func:`conversation_version` aggregates, because a thread's markup follows its
message rows. ``Max("updated_at")`` alone is not enough: delete the most
recently touched row and the maximum walks *backwards* to a value the client is
already holding, so the stale render survives the change that removed it.
Pairing it with ``Count("id")`` closes that.

:func:`list_version` hashes the values the rows are about to print instead. An
aggregate over conversations kept missing changes that live somewhere else — a
refused send writes a message without touching the conversation, a rename moves
a contact — and each miss was a client stuck on a 304 for ever. Hashing the
render inputs makes the token right by construction rather than right as long
as somebody remembers to add the next input to it.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from django.db.models import Count, DateTimeField, Exists, F, Max, OuterRef, Q, QuerySet, Subquery, Value
from django.db.models.functions import Coalesce
from django.utils.timesince import timesince

from apps.contacts.models import Contact
from apps.inbox.models import ConversationRead
from apps.messaging.models import Conversation, ConversationState, Message, MessageDirection

__all__ = [
    "ASSIGNEE_ME",
    "ASSIGNEE_UNASSIGNED",
    "LIST_LIMIT",
    "MAX_THREAD_MESSAGES",
    "PAGE_SIZE",
    "UNREAD_BADGE_CAP",
    "conversation_version",
    "conversations_for",
    "last_messages_by_conversation",
    "list_version",
    "live_execution_for",
    "thread_messages",
    "unread_count_for",
    "with_unread",
]

#: The two assignee filters that are not a member id.
ASSIGNEE_ME = "me"
ASSIGNEE_UNASSIGNED = "unassigned"

#: The cursor a member who has never opened a conversation is treated as
#: holding. Any real message is newer, so every thread starts unread.
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

#: How many messages one thread page carries, and how much each "load earlier"
#: adds to the window.
PAGE_SIZE = 50

#: The largest window a thread will render. The region is polled every three
#: seconds, so this bounds both what a reader can grow by clicking and what a
#: hand-edited query string can ask for.
MAX_THREAD_MESSAGES = 1000

#: The most the sidebar badge will ever report. The count runs on every
#: authenticated page render, so it is deliberately bounded rather than exact —
#: see :func:`unread_count_for`.
UNREAD_BADGE_CAP = 99

#: How many conversations the list renders at once. The list is a working
#: surface rather than an archive — the filters are how a hundred-plus-thread
#: workspace narrows down — and an unbounded list is also an unbounded poll
#: response every three seconds.
LIST_LIMIT = 100


def conversations_for(
    workspace: Any,
    *,
    viewer: Any,
    state: str = "",
    connection_id: Any = None,
    assignee: str = "",
) -> QuerySet[Conversation]:
    """The conversation list, filtered and ordered by recency (SPEC §14).

    ``-last_message_at`` with ``state`` and ``workspace`` is exactly
    ``conv_ws_state_last_idx``, the index ``apps.messaging.models`` declares
    "SPEC §14's inbox list" for.
    """
    rows = (
        Conversation.objects.for_workspace(workspace)
        .select_related("contact", "channel_connection", "assignee")
        # nulls_last is not decoration. ``open_conversation`` creates a thread
        # with ``last_message_at = None`` and only ``_touch`` ever fills it, so
        # a conversation opened before its first message — a flow that opens one
        # then fails to send, an API caller — has NULL. Postgres orders NULLs
        # FIRST for DESC, which pinned every message-less thread above every
        # real one at the top of the inbox.
        .order_by(F("last_message_at").desc(nulls_last=True), "-created_at")
    )
    if state in (ConversationState.OPEN, ConversationState.DONE):
        rows = rows.filter(state=state)
    if connection_id:
        parsed = _as_uuid(connection_id)
        rows = rows.filter(channel_connection_id=parsed) if parsed else rows.none()
    if assignee == ASSIGNEE_ME:
        rows = rows.filter(assignee=viewer)
    elif assignee == ASSIGNEE_UNASSIGNED:
        rows = rows.filter(assignee__isnull=True)
    elif assignee:
        parsed = _as_uuid(assignee)
        rows = rows.filter(assignee_id=parsed) if parsed else rows.none()
    return with_unread(rows, workspace=workspace, viewer=viewer)


def _as_uuid(value: Any) -> UUID | None:
    """A filter value from the query string, or None if it is not an id.

    Both id filters go through here. They are query-string fragments, and
    handing a non-UUID to a UUID column raises — a 500 anyone can reach with a
    stale bookmark, on the two endpoints the page polls every three seconds. An
    unparseable id filters nothing, which is what an unknown-but-valid one would
    have done anyway.
    """
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def with_unread(rows: QuerySet[Conversation], *, workspace: Any, viewer: Any) -> QuerySet[Conversation]:
    """Annotate ``unread``: has the contact written since this member last read?

    Two correlated subqueries rather than a join, because a member has a read
    row for only the conversations they have opened and a ``LEFT JOIN`` on a
    table filtered by ``user`` is the classic way to accidentally drop every
    other row.
    """
    cursor = ConversationRead.objects.for_workspace(workspace).filter(conversation=OuterRef("pk"), user=viewer)
    inbound_since = Message.objects.for_workspace(workspace).filter(
        conversation=OuterRef("pk"),
        direction=MessageDirection.IN,
        # ``read_cursor`` is annotated one step earlier so this OuterRef has
        # something typed to point at; the explicit output_field on both sides
        # is what stops Coalesce from having to guess across a Subquery.
        created_at__gt=Coalesce(OuterRef("read_cursor"), Value(_EPOCH), output_field=DateTimeField()),
    )
    return rows.annotate(
        read_cursor=Subquery(cursor.values("last_read_at")[:1], output_field=DateTimeField())
    ).annotate(unread=Exists(inbound_since))


def unread_count_for(workspace: Any, viewer: Any) -> int:
    """How many open conversations are waiting on this member, up to the cap.

    **Saturates at** :data:`UNREAD_BADGE_CAP`. This runs in the shell's context
    processor, so it is on the critical path of every authenticated page in the
    product, not only the inbox — and unlike issue #7's notification count,
    which is one indexed ``count()``, this one is a correlated ``EXISTS`` per
    open conversation. An exact answer would mean scanning every open thread in
    the workspace on every page load; the slice lets Postgres stop as soon as it
    has enough rows to fill a two-digit badge, which is all the badge can say.
    """
    rows = Conversation.objects.for_workspace(workspace).filter(state=ConversationState.OPEN)
    # Q() rather than a keyword argument: ``unread`` is an annotation, and
    # django-stubs resolves keyword lookups against the model's real fields.
    unread = with_unread(rows, workspace=workspace, viewer=viewer).filter(Q(unread=True))
    return len(unread.values_list("pk", flat=True)[:UNREAD_BADGE_CAP])


def last_messages_by_conversation(workspace: Any, conversations: list[Conversation]) -> dict[Any, Message]:
    """The newest message of each listed conversation: one query, one row each.

    ``DISTINCT ON`` rather than "fetch them all and keep the last per key". The
    naive form is one query too, which is what makes it easy to ship — but it
    materialises *every message of every listed conversation* to keep a hundred
    preview lines. A hundred threads averaging five hundred messages is fifty
    thousand model instances built and thrown away, on every list render, for
    every open tab.

    Postgres-only, which this project already is (SPEC §2, and CI runs Postgres
    16). The ``order_by`` prefix has to match the ``distinct`` expression, so
    recency is the second term and the tie-break the third.
    """
    ids = [conversation.pk for conversation in conversations]
    if not ids:
        return {}
    rows = (
        Message.objects.for_workspace(workspace)
        .filter(conversation_id__in=ids)
        .order_by("conversation_id", "-created_at", "-id")
        .distinct("conversation_id")
    )
    return {message.conversation_id: message for message in rows}


def thread_messages(workspace: Any, conversation: Conversation, *, limit: int) -> tuple[list[Message], bool]:
    """The newest ``limit`` messages, oldest-first, plus whether more exist above.

    A **growing window** rather than a ``before=<timestamp>`` cursor, and the
    reason is the poll. This region refreshes every three seconds; a cursor
    describes one page in the middle of the history, so the very next poll —
    which knows nothing about it — would swap the newest page back and throw the
    reader out of the history they had just opened. A window anchored at the
    newest message is a superset of what the poll would render anyway, so
    "load earlier" and "a message arrived" compose instead of fighting.

    It also disposes of a cursor bug rather than fixing one: ``created_at__lt``
    against an ordering that tie-breaks on ``id`` skips *both* rows when two
    messages share a timestamp, so a message could exist in the thread and never
    be reachable. There is no cursor here to get that wrong.

    Read as ``(conversation, created_at)`` descending — ``message_conv_created_idx``
    backwards — then reversed in Python, which is free at these sizes and beats
    an ascending ``OFFSET`` on exactly the long threads this exists for.
    """
    rows = Message.objects.for_workspace(workspace).filter(conversation=conversation)
    page = list(rows.order_by("-created_at", "-id")[: limit + 1])
    has_more = len(page) > limit
    page = page[:limit]
    page.reverse()
    return page, has_more


def list_version(rendered: list[dict[str, Any]]) -> tuple[Any, ...]:
    """The conversation list's ETag, read off the rows it is about to render.

    Derived from the payload rather than from aggregates over the tables behind
    it, because the two kept disagreeing. A refused send is the case that broke
    it: ``messaging._failed`` writes a message row and deliberately does *not*
    call ``_touch`` — a refusal is not thread recency — so the preview line
    changed while ``Max(conversation.updated_at)`` sat still, and a client
    holding the old tag went on being told nothing had happened. Contact and
    member renames were the same shape from a different direction: they move a
    row this token never looked at.

    Hashing the values the template prints ends that class of bug rather than
    fixing one instance of it: anything the markup shows is in the token by
    construction, and anything it does not show cannot churn it. The rows are
    already in memory — :func:`conversations_for` and
    :func:`last_messages_by_conversation` between them are two bounded queries —
    so this costs a join of about a hundred short strings, and an unchanged poll
    still skips the template, which is the expensive half.
    """
    return tuple(
        (
            str(row["conversation"].pk),
            row["conversation"].state,
            row["conversation"].contact.display_name,
            row["conversation"].channel_connection.platform,
            # The assignee's *name*, because that is what the chip prints — an
            # id would hold still through a rename.
            row["conversation"].assignee.display_name if row["conversation"].assignee else "",
            # The relative string, not the timestamp behind it. "5 minutes ago"
            # goes wrong on its own while the row it describes never moves, and
            # a token built from last_message_at would answer 304 to that. This
            # way the list refreshes when the text would actually change —
            # about once a minute on a quiet workspace, not every three seconds.
            timesince(row["conversation"].last_message_at) if row["conversation"].last_message_at else "",
            row["preview"],
            row["last_internal"],
            row["unread"],
        )
        for row in rendered
    )


def conversation_version(workspace: Any, conversation: Conversation) -> tuple[Any, ...]:
    """The database half of a thread's ETag.

    ``conversation.updated_at`` is in it because the pause banner, the state and
    the assignee all render inside the polled region and none of them touch a
    message row.
    """
    messages = (
        Message.objects.for_workspace(workspace)
        .filter(conversation=conversation)
        .aggregate(latest=Max("updated_at"), total=Count("id"))
    )
    return (conversation.updated_at, messages["latest"], messages["total"])


def live_execution_for(workspace: Any, contact: Contact) -> Any:
    """The automation currently holding this contact, if any (SPEC §9.2).

    Imported inside the function: ``apps.flows`` imports messaging through a
    facade shim precisely to avoid a module-level dependency between the two,
    and the inbox has no reason to add one at import time either.
    """
    from apps.flows.models import LIVE_STATUSES, FlowExecution

    return (
        FlowExecution.objects.for_workspace(workspace)
        .filter(contact=contact, status__in=sorted(LIVE_STATUSES))
        .select_related("flow")
        .first()
    )
