"""Every read the inbox performs. No writes live here.

Two things in this module are load-bearing and easy to get subtly wrong:

**Unread.** ``Conversation.last_message_at`` moves for *any* message — the
agent's own reply and an internal note included — so "last_message_at is newer
than my cursor" would light the badge the instant an agent answered. The
question SPEC §14 is actually asking is whether the *contact* has said something
since this member last looked, so :func:`with_unread` tests for an inbound
message newer than the cursor and nothing else. That is also what the issue
means by keeping notes out of the contact-visible counts.

**The version token.** The pollers need a value that changes when, and only
when, the rendered markup would. ``Max("updated_at")`` alone is not it: delete
the most recently touched row and the maximum walks *backwards* to a value the
client is already holding, so the stale render survives the change that removed
it. Pairing it with ``Count("id")`` closes that, which is why every token here
is built from both.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from django.db.models import Count, DateTimeField, Exists, Max, OuterRef, Q, QuerySet, Subquery, Value
from django.db.models.functions import Coalesce

from apps.contacts.models import Contact
from apps.inbox.models import ConversationRead
from apps.messaging.models import Conversation, ConversationState, Message, MessageDirection

__all__ = [
    "ASSIGNEE_ME",
    "ASSIGNEE_UNASSIGNED",
    "LIST_LIMIT",
    "PAGE_SIZE",
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

#: How many messages one thread page carries.
PAGE_SIZE = 50

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
        .order_by("-last_message_at", "-created_at")
    )
    if state in (ConversationState.OPEN, ConversationState.DONE):
        rows = rows.filter(state=state)
    if connection_id:
        rows = rows.filter(channel_connection_id=connection_id)
    if assignee == ASSIGNEE_ME:
        rows = rows.filter(assignee=viewer)
    elif assignee == ASSIGNEE_UNASSIGNED:
        rows = rows.filter(assignee__isnull=True)
    elif assignee:
        # Parsed rather than passed through: the value is a query-string
        # fragment, and handing a non-UUID to a UUID column is a 500 anyone can
        # trigger with a bookmark. An unparseable one filters nothing, which is
        # the same thing an unknown-but-valid id would do.
        try:
            rows = rows.filter(assignee_id=UUID(assignee))
        except ValueError:
            rows = rows.none()
    return with_unread(rows, workspace=workspace, viewer=viewer)


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
    """How many open conversations are waiting on this member. Sidebar badge."""
    rows = Conversation.objects.for_workspace(workspace).filter(state=ConversationState.OPEN)
    # Q() rather than a keyword argument: ``unread`` is an annotation, and
    # django-stubs resolves keyword lookups against the model's real fields.
    return with_unread(rows, workspace=workspace, viewer=viewer).filter(Q(unread=True)).count()


def last_messages_by_conversation(workspace: Any, conversations: list[Conversation]) -> dict[Any, Message]:
    """The newest message of each listed conversation, in one query.

    A per-row ``.messages.last()`` would be N queries for a hundred-row list,
    and the preview line needs one message per row. Ordered oldest-first so the
    dict ends up holding the newest.
    """
    ids = [conversation.pk for conversation in conversations]
    if not ids:
        return {}
    rows = Message.objects.for_workspace(workspace).filter(conversation_id__in=ids).order_by("created_at")
    return {message.conversation_id: message for message in rows}


def thread_messages(
    workspace: Any, conversation: Conversation, *, before: datetime | None = None
) -> tuple[list[Message], bool]:
    """One page of history, oldest-first, plus whether more exists above it.

    Paginated *upward*: the newest page is the default and ``before`` walks back
    through it. The query is ``(conversation, created_at)`` descending, which is
    ``message_conv_created_idx`` read backwards, then reversed in Python — a
    page is fifty rows, so the sort is free and the alternative (ordering
    ascending with an offset) degrades on exactly the long threads this exists
    for.
    """
    rows = Message.objects.for_workspace(workspace).filter(conversation=conversation)
    if before is not None:
        rows = rows.filter(created_at__lt=before)
    page = list(rows.order_by("-created_at", "-id")[: PAGE_SIZE + 1])
    has_more = len(page) > PAGE_SIZE
    page = page[:PAGE_SIZE]
    page.reverse()
    return page, has_more


def list_version(workspace: Any, rows: QuerySet[Conversation], viewer: Any) -> tuple[Any, ...]:
    """The parts of the conversation list's ETag that come from the database.

    The viewer's read rows are in there because the unread dot is part of the
    markup: without them, marking a thread read would leave every other tab
    holding an ETag that still says "unread" and no reason to refetch.
    """
    conversations = rows.aggregate(latest=Max("updated_at"), total=Count("id"))
    reads = (
        ConversationRead.objects.for_workspace(workspace)
        .filter(user=viewer)
        .aggregate(latest=Max("updated_at"), total=Count("id"))
    )
    return (conversations["latest"], conversations["total"], reads["latest"], reads["total"])


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
