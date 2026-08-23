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
from apps.inbox.models import (
    ConversationLabel,
    ConversationLabelLink,
    ConversationRead,
    DeferredStatus,
    InboxReminder,
    ScheduledReply,
)
from apps.messaging.models import Conversation, ConversationState, Message, MessageDirection
from apps.queueing.models import ActionStatus

__all__ = [
    "ASSIGNEE_ME",
    "ASSIGNEE_UNASSIGNED",
    "LIST_LIMIT",
    "MAX_THREAD_MESSAGES",
    "PAGE_SIZE",
    "UNREAD_BADGE_CAP",
    "conversation_version",
    "conversations_for",
    "conversations_with_pending",
    "DRY_RUN_SAMPLE",
    "deferred_version",
    "dry_run",
    "failed_replies_for",
    "label_usage",
    "labels_by_conversation",
    "labels_for",
    "last_messages_by_conversation",
    "list_version",
    "pending_reminders_for",
    "pending_replies_for",
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
    label: str = "",
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
    if label:
        parsed = _as_uuid(label)
        # ``Exists`` rather than a join: a join to the link table multiplies the
        # row out once per label and would need a ``distinct()`` that then has to
        # agree with the ``order_by`` above. The correlated probe is also what
        # ``labellink_ws_label_idx`` is shaped for.
        rows = (
            rows.filter(
                Exists(
                    ConversationLabelLink.objects.for_workspace(workspace).filter(
                        conversation=OuterRef("pk"), label_id=parsed
                    )
                )
            )
            if parsed
            else rows.none()
        )
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
            # The chips as they print — names and colours, not ids. A rename or a
            # recolour changes the markup while every id holds still, which is
            # the same argument the assignee line above makes.
            tuple((chip.name, chip.color) for chip in row.get("labels", ())),
            # A boolean, deliberately. The row prints a glyph, not a countdown:
            # a due time in here would re-derive from the clock and churn the
            # whole list every minute, for every open tab, over markup that does
            # not show it. The thread token is where the countdown belongs.
            row.get("has_pending", False),
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


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------


def labels_for(workspace: Any) -> QuerySet[ConversationLabel]:
    """This workspace's label palette, for the filter bar and the pickers."""
    return ConversationLabel.objects.for_workspace(workspace).order_by("name")


def labels_by_conversation(workspace: Any, conversations: list[Conversation]) -> dict[Any, list[Any]]:
    """The labels on each of these threads, in one query.

    Shaped like :func:`last_messages_by_conversation` and for the same reason:
    the caller has already sliced the page to ``LIST_LIMIT``, and
    ``prefetch_related`` on a sliced queryset re-runs the slice as a subquery
    rather than reusing the rows in hand. One ``IN`` over a hundred ids, ordered
    so the chips print alphabetically without the caller sorting.
    """
    if not conversations:
        return {}
    rows = (
        ConversationLabelLink.objects.for_workspace(workspace)
        .filter(conversation_id__in=[conversation.pk for conversation in conversations])
        .select_related("label")
        .order_by("conversation_id", "label__name")
    )
    grouped: dict[Any, list[Any]] = {}
    for link in rows:
        grouped.setdefault(link.conversation_id, []).append(link.label)
    return grouped


def conversations_with_pending(workspace: Any, conversations: list[Conversation]) -> set[Any]:
    """Which of these threads have deferred work still waiting, in one query.

    "Waiting" is the queue's answer, not this app's column: a pending row whose
    action was cancelled by ``contacts.activity.stand_down`` is not going to
    fire, and advertising it would be a promise the thread cannot keep.
    """
    if not conversations:
        return set()
    ids = [conversation.pk for conversation in conversations]
    found: set[Any] = set()
    for model in (InboxReminder, ScheduledReply):
        found.update(
            model.objects.for_workspace(workspace)
            .filter(conversation_id__in=ids, status=DeferredStatus.PENDING, action__status=ActionStatus.PENDING)
            .values_list("conversation_id", flat=True)
        )
    return found


# ---------------------------------------------------------------------------
# Deferred work on one thread
# ---------------------------------------------------------------------------


def pending_reminders_for(workspace: Any, conversation: Conversation) -> list[InboxReminder]:
    """Reminders on this thread that the queue will actually fire."""
    return list(
        InboxReminder.objects.for_workspace(workspace)
        .filter(conversation=conversation, status=DeferredStatus.PENDING, action__status=ActionStatus.PENDING)
        .select_related("recipient")
        .order_by("remind_at")
    )


def pending_replies_for(workspace: Any, conversation: Conversation) -> list[ScheduledReply]:
    """Replies queued on this thread that the queue will actually send."""
    return list(
        ScheduledReply.objects.for_workspace(workspace)
        .filter(conversation=conversation, status=DeferredStatus.PENDING, action__status=ActionStatus.PENDING)
        .order_by("send_at")
    )


def failed_replies_for(workspace: Any, conversation: Conversation) -> list[ScheduledReply]:
    """Replies that came due and were refused.

    Shown until an operator dismisses them, because "never a silent drop" means
    the thread has to carry the failure, not just the notification that a
    logged-out agent may never see.
    """
    return list(
        ScheduledReply.objects.for_workspace(workspace)
        .filter(conversation=conversation, status=DeferredStatus.FAILED)
        .order_by("-send_at")
    )


def deferred_version(workspace: Any, conversation: Conversation) -> tuple[Any, ...]:
    """The thread token's half for reminders and scheduled replies.

    ``Max("updated_at")`` **and** ``Count("id")``, the delete-safe pair this
    module's docstring argues for: cancelling moves ``updated_at``, and the count
    catches the case a ``Max`` cannot see. It is also why cancelling sets a
    status rather than deleting the row.

    Deliberately not folded into :func:`conversation_version`: the conversation
    list does not print any of this, and a token that moved with it would refresh
    every row in the inbox because one thread's countdown ticked.
    """
    reminders = (
        InboxReminder.objects.for_workspace(workspace)
        .filter(conversation=conversation)
        .aggregate(latest=Max("updated_at"), total=Count("id"))
    )
    replies = (
        ScheduledReply.objects.for_workspace(workspace)
        .filter(conversation=conversation)
        .aggregate(latest=Max("updated_at"), total=Count("id"))
    )
    return (reminders["latest"], reminders["total"], replies["latest"], replies["total"])


def label_usage(workspace: Any) -> dict[Any, int]:
    """How many threads carry each label, in one grouped query.

    The settings list shows it because "delete" is destructive and the number is
    the only thing that says how destructive: a label on four hundred threads and
    a label on none look identical without it.
    """
    rows = ConversationLabelLink.objects.for_workspace(workspace).values("label_id").annotate(total=Count("id"))
    return {row["label_id"]: row["total"] for row in rows}


#: How many recent messages the rule dry-run scores. SPEC's "test against last 50
#: messages", spelled once.
DRY_RUN_SAMPLE = 50


def dry_run(workspace: Any, condition: dict[str, Any]) -> tuple[list[Message], int]:
    """Score a condition against the messages this workspace last received.

    Two properties the settings page's claim rests on.

    **It is the same matcher.** :func:`apps.inbox.rules.matches_shallow` and
    :class:`~apps.inbox.rules.RuleInput` are what the ``post_persist`` hook runs;
    the only difference here is which of ``RuleInput``'s two constructors built
    the input. Anything less and "the dry-run matches live behaviour" would be a
    claim rather than a property.

    **It asks the condition engine once, not once per message.** The contact half
    goes through :func:`apps.contacts.conditions.evaluate_many`, so fifty
    messages cost one query rather than fifty. That is also why the halves are
    split: ``matches()`` would fold the contact clause into the per-message loop.

    The sample is inbound, non-internal and recent, because that is the only
    traffic the hook ever sees — scoring the team's own replies would look
    broken. It is **not** a replay: today's contact state is evaluated against
    old messages, and the page says so rather than implying otherwise.
    """
    from apps.contacts.conditions import evaluate_many
    from apps.inbox.rules import RuleInput, compile_rule, matches_shallow

    sample = list(
        Message.objects.for_workspace(workspace)
        .filter(direction=MessageDirection.IN, internal=False)
        .select_related("conversation", "conversation__contact", "channel_connection")
        .order_by("-created_at")[:DRY_RUN_SAMPLE]
    )
    if not sample:
        return [], 0

    compiled = compile_rule(_LooseRule(condition))
    shallow = [message for message in sample if matches_shallow(compiled, RuleInput.from_message(message))]
    if not shallow or compiled.contact_filter is None:
        return shallow, len(sample)

    contacts = {message.conversation.contact for message in shallow}
    allowed = evaluate_many(workspace, contacts, compiled.contact_filter)
    return [message for message in shallow if message.conversation.contact_id in allowed], len(sample)


class _LooseRule:
    """An unsaved condition, shaped like the row ``compile_rule`` expects.

    The dry-run scores a document that has been typed but not stored — that is
    the point of it — and building an unsaved ``InboxRule`` just to read two
    attributes off it would put a model instance in a path that never touches
    the database.
    """

    pk = None

    def __init__(self, condition: dict[str, Any]) -> None:
        self.condition_json = condition
        # Per instance, not a class attribute. A mutable default on the class is
        # shared by every instance ever made, and the first caller to append to
        # it would leak into every later dry-run in the process.
        self.actions_json: list[Any] = []
