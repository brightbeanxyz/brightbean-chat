"""The inbox's own state: read cursors, labels, rules, and deferred work.

Everything the inbox *shows* about a conversation lives in ``apps.messaging`` and
is read-only here — ROADMAP contract 1 makes ``messaging/services.py`` the only
way to mutate a conversation or a message, and contract 3 makes
``automation_paused_until`` messaging's to write. What messaging does *not* have
is any notion of who has read what, which threads carry which labels, what an
operator asked to be reminded about, or what they queued to send later.

So all of that lives here, in the app that needs it. These are the inbox's own
bookkeeping about its own operators rather than tenant-visible conversation
state, which is why adding them does not cross the facade boundary. SPEC §5
groups ``conversation_label`` and ``inbox_rule`` under its *messaging* heading;
that section is a data-model sketch, not an app assignment, and putting them here
keeps every migration this issue ships inside an app no sibling workstream
touches.

Two rules the rows below all obey.

*Workspace is derived, never set by a caller.* ``ConversationScopedModel`` takes
it from the conversation and checks the peer foreign key against the same
workspace, for the reason ``apps.contacts.models.ContactScopedModel`` spells out
at length: ``link.workspace``, ``link.conversation.workspace`` and
``link.label.workspace`` are three separate answers to "whose row is this?", and
a form field naming another tenant's label is an IDOR no URL fuzzer reaches.

*Deferred work is cancelled by status, never deleted.* The thread's ETag is a
``Max(updated_at)`` and a ``Count(id)`` over these tables (``apps.inbox.selectors``),
and a ``Max`` cannot see a row that is gone.
"""

from typing import TYPE_CHECKING, Any, ClassVar

from django.conf import settings
from django.db import models
from django.db.models.functions import Lower

from apps.common.scoping import WorkspaceScopedModel
from apps.common.validators import validate_hex_color

__all__ = [
    "DEFAULT_LABEL_COLOR",
    "MAX_LABELS_PER_CONVERSATION",
    "MAX_REMINDER_NOTE_CHARS",
    "ConversationLabel",
    "ConversationLabelLink",
    "ConversationRead",
    "ConversationScopedModel",
    "DeferredStatus",
    "InboxRule",
    "InboxRuleApplication",
    "InboxReminder",
    "ScheduledReply",
    "WorkspaceMismatchError",
]

#: What a label with no colour of its own renders as. A token name, not a hex
#: value: :mod:`apps.inbox.rendering` maps it to a CSS custom property, and the
#: fallback must never be a string that could be mistaken for stored data.
DEFAULT_LABEL_COLOR = "#64748B"

#: How many labels one thread may carry. The conversation list renders chips for
#: every row it shows, and ``apps/inbox/tests/test_hostile_content.py`` caps the
#: whole rendered list at 20 kB — a hundred rows carrying unbounded chips is the
#: shape that trips it.
MAX_LABELS_PER_CONVERSATION = 20

#: A reminder note is operator-authored and renders in a notification body.
MAX_REMINDER_NOTE_CHARS = 500


class WorkspaceMismatchError(ValueError):
    """A row was built from objects belonging to two different workspaces."""


class DeferredStatus(models.TextChoices):
    """Where a reminder or a scheduled reply got to.

    Deliberately **not** the whole answer to "will this fire?" — see
    :attr:`ScheduledReply.will_fire`. The queue row is the schedule; this column
    records the outcomes a ``ScheduledAction`` cannot express, and ``CANCELLED``
    is what an operator's cancel writes here *as well as* on the queue row.
    """

    PENDING = "pending", "Pending"
    SENT = "sent", "Sent"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"
    #: A failure an operator has read and taken off the thread. Distinct from
    #: ``CANCELLED`` because "it never went out because I called it off" and "it
    #: never went out because the platform refused it, and I have seen that" are
    #: different facts, and the second one is the one an audit wants back.
    DISMISSED = "dismissed", "Dismissed"


class ConversationScopedModel(WorkspaceScopedModel):
    """Abstract base for rows that hang off a ``Conversation`` and inherit its tenancy.

    The sibling of :class:`apps.contacts.models.ContactScopedModel`, and written
    from it deliberately rather than beside it: the argument for deriving
    ``workspace`` instead of accepting it, and for checking one peer foreign key
    against the same workspace, is identical, and so are the two traps (a falsy
    ``update_fields``, and ``bulk_create`` bypassing ``save()`` entirely).

    ``bulk_create`` matters here in a way it did not there: applying several
    labels at once is one ``bulk_create(..., ignore_conflicts=True)``, so
    :meth:`ConversationLabelLink.for_conversation` builds those rows through a
    constructor that does the derivation, and nothing else in this app
    bulk-creates.

    Declares no managers, so ``all_objects`` keeps the lowest creation counter
    and ``apps.common.checks`` (``common.E004``) stays satisfied.
    """

    #: Name of the foreign key whose workspace must agree with the conversation's.
    #: Empty when the row's other half is a user rather than a tenant object — a
    #: read cursor and a reminder recipient are people, and a person has no
    #: workspace to disagree with. Membership is checked at the view, which is
    #: where the caller and the permission are.
    peer_field: ClassVar[str] = ""

    if TYPE_CHECKING:
        # Declared by every concrete subclass. Annotated rather than assigned so
        # Django's model machinery never sees a non-field attribute here.
        conversation: Any

    class Meta:
        abstract = True

    def full_clean(self, *args: Any, **kwargs: Any) -> None:
        """Derive the workspace *before* validating, not only before saving.

        ``clean_fields()`` runs first inside ``full_clean`` and would report
        ``workspace: This field cannot be null`` — a message about a column no
        caller is allowed to set, on a row whose tenancy is never in doubt. The
        services call ``full_clean`` so a constraint violation arrives as a
        sentence rather than an ``IntegrityError``; this is what keeps that
        worth doing.
        """
        self._derive_workspace()
        super().full_clean(*args, **kwargs)

    def save(self, *args: Any, **kwargs: Any) -> None:
        self._derive_workspace()
        if self.peer_field:
            peer = getattr(self, self.peer_field)
            if peer is not None and peer.workspace_id != self.conversation.workspace_id:
                raise WorkspaceMismatchError(
                    f"That {peer._meta.verbose_name} belongs to a different workspace than the conversation."
                )
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            # Only widen a non-empty set. Django reads a falsy ``update_fields``
            # as "save nothing" and returns before touching the database, so
            # adding ``workspace`` to an empty one would turn a documented no-op
            # into a real UPDATE.
            widened = set(update_fields)
            kwargs["update_fields"] = widened | {"workspace"} if widened else widened
        super().save(*args, **kwargs)

    def _derive_workspace(self) -> None:
        self.workspace_id = self.conversation.workspace_id


class ConversationRead(ConversationScopedModel):
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


class ConversationLabel(WorkspaceScopedModel):
    """A label a team puts on a thread (SPEC §5, §14).

    Distinct from ``apps.contacts.models.Tag``, which is a property of a
    *person*: "VIP" follows a contact across every channel they use, while
    "waiting on shipping" is true of one thread and stops being true when it is
    answered. The inbox already edits contact tags from the thread sidebar; this
    is the other axis, and conflating them would mean closing a conversation had
    to reach into the CRM.
    """

    name = models.CharField(max_length=60)
    #: Rendered into an inline ``style`` attribute, so it is vetted **again** at
    #: render time by :mod:`apps.inbox.rendering`. This validator is the write
    #: half; a row can still reach the database through a migration or a shell.
    color = models.CharField(
        max_length=7,
        blank=True,
        default=DEFAULT_LABEL_COLOR,
        validators=[validate_hex_color],
    )

    class Meta:
        db_table = "inbox_conversation_label"
        ordering = ["name"]
        constraints = [
            # Lower(name), like contacts.Tag: full_clean() validates expression
            # constraints, so the form reports the clash rather than a 500.
            models.UniqueConstraint(Lower("name"), "workspace", name="label_unique_name_per_workspace"),
        ]

    def __str__(self) -> str:
        return self.name


class ConversationLabelLink(ConversationScopedModel):
    """The thread ↔ label join (SPEC §5's ``conversation_label_link``)."""

    peer_field = "label"

    conversation = models.ForeignKey(
        "messaging.Conversation",
        on_delete=models.CASCADE,
        related_name="label_links",
    )
    label = models.ForeignKey(ConversationLabel, on_delete=models.CASCADE, related_name="links")
    #: Null when a rule applied it rather than a person. Kept because "who put
    #: this here" is the first question asked of an unexpected label, and
    #: ``SET_NULL`` because a departing member must not take the label with them.
    applied_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="applied_conversation_labels",
    )

    class Meta:
        db_table = "inbox_conversation_label_link"
        constraints = [
            # SPEC §5's unique-together, and what makes applying a label
            # idempotent: the inbox rules hook re-runs on a deferred replay and
            # relies on ignore_conflicts rather than on a read-then-write.
            models.UniqueConstraint(fields=["conversation", "label"], name="labellink_unique_conv_label"),
        ]
        indexes = [
            # "Which threads carry label X" — the list filter. workspace leads
            # because every query it serves is already scoped.
            models.Index(fields=["workspace", "label"], name="labellink_ws_label_idx"),
        ]

    def __str__(self) -> str:
        return f"label {self.label_id} on {str(self.conversation_id)[:8]}"

    @classmethod
    def unsaved(cls, *, conversation: Any, label: Any, applied_by: Any = None) -> "ConversationLabelLink":
        """A link with its tenancy already derived, for ``bulk_create``.

        ``bulk_create`` does not call ``save()``, so the derivation and the
        workspace check that make these rows safe would be skipped — which is
        exactly the caveat ``ContactScopedModel`` documents and leaves to its
        callers. Applying several labels at once is worth one query, so this
        does the derivation in a constructor instead of forbidding the bulk path.
        """
        if label.workspace_id != conversation.workspace_id:
            raise WorkspaceMismatchError("That label belongs to a different workspace than the conversation.")
        return cls(
            workspace_id=conversation.workspace_id,
            conversation=conversation,
            label=label,
            applied_by=applied_by,
        )


class InboxRule(WorkspaceScopedModel):
    """One "when this arrives, do that" rule (SPEC §5, §14).

    **Cumulative, unlike a trigger.** ``apps.flows.triggers.matching.match``
    returns the first trigger that matches and stops; every enabled rule that
    matches an inbound message applies here, in ``priority`` order. That is what
    lets one rule label by channel and another assign by keyword without either
    knowing about the other, and it is why a rule cannot consume the event.

    ``condition_json`` and ``actions_json`` are validated by
    :mod:`apps.inbox.rules` on the way in — this document is parsed on every
    inbound event on every connection in the deployment, so its caps are a
    latency budget as much as SECURITY-BASELINE §7's mass-assignment guard.
    """

    name = models.CharField(max_length=120)
    condition_json = models.JSONField(default=dict, blank=True)
    actions_json = models.JSONField(default=list, blank=True)
    enabled = models.BooleanField(default=True)
    #: Lower runs first, and the reorder endpoint renormalises the workspace's
    #: rules to 0, 10, 20… so there is always room to drop one between two
    #: others — the convention ``apps.flows.triggers.services.move_trigger``
    #: established for the same problem.
    priority = models.IntegerField(default=0)

    class Meta:
        db_table = "inbox_rule"
        ordering = ["priority", "name"]
        indexes = [
            # The hook's one query per inbound event: this workspace's enabled
            # rules, in priority order.
            models.Index(fields=["workspace", "enabled", "priority"], name="inboxrule_ws_enabled_idx"),
        ]

    def __str__(self) -> str:
        return self.name


class InboxRuleApplication(ConversationScopedModel):
    """A ledger row: this rule has already acted on this event.

    The reason the hook can be re-entered safely. ``post_persist`` is replayed
    in full when *any* hook at that stage defers (``apps.flows.triggers.pipeline``
    hands the whole stage to the worker), so every action a rule takes has to be
    exactly-once per ``(rule, event)`` — and giving each action its own
    idempotency story means three stories that can each be wrong separately.
    One insert, whose unique constraint arbitrates, covers all of them.

    ``event_ref`` is a digest of the provider's event id rather than the id
    itself, for the reasons
    :func:`apps.flows.triggers.handlers.route_idempotency_key` gives: the value
    is attacker-controlled and unbounded, and ``(connection, provider_event_id)``
    is already unique upstream, so the digest identifies the event exactly.

    It is also the audit the rule builder's dry-run wants — "which rules fired on
    this message" is a question this table already answers.
    """

    peer_field = "rule"

    conversation = models.ForeignKey(
        "messaging.Conversation",
        on_delete=models.CASCADE,
        related_name="rule_applications",
    )
    rule = models.ForeignKey(InboxRule, on_delete=models.CASCADE, related_name="applications")
    event_ref = models.CharField(max_length=64)

    class Meta:
        db_table = "inbox_rule_application"
        constraints = [
            models.UniqueConstraint(
                fields=["conversation", "rule", "event_ref"],
                name="ruleapp_unique_conv_rule_event",
            ),
        ]
        indexes = [
            models.Index(fields=["workspace", "rule"], name="ruleapp_ws_rule_idx"),
        ]

    def __str__(self) -> str:
        return f"rule {self.rule_id} on {self.event_ref}"


class DeferredWorkModel(ConversationScopedModel):
    """What a reminder and a scheduled reply have in common: a clock and a queue row.

    ``action`` is ``SET_NULL`` rather than ``CASCADE`` on purpose. Queue
    housekeeping prunes ``scheduled_action``, and a cascade would delete an
    agent's scheduled reply because a maintenance job tidied up after it.
    """

    #: The queue row that will do the work. **This, not** :attr:`status`, **is
    #: the answer to "will it fire?"** — ``apps.contacts.activity.stand_down``
    #: cancels every pending action naming a contact when that contact is soft
    #: deleted, without knowing this table exists, so a row trusting only its own
    #: column would advertise a reminder in the thread for ever.
    action = models.ForeignKey(
        "queueing.ScheduledAction",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    status = models.CharField(max_length=16, choices=DeferredStatus.choices, default=DeferredStatus.PENDING)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        abstract = True

    @property
    def will_fire(self) -> bool:
        """Whether the queue still holds work for this row."""
        from apps.queueing.models import ActionStatus

        return (
            self.status == DeferredStatus.PENDING
            and self.action is not None
            and self.action.status == ActionStatus.PENDING
        )


class InboxReminder(DeferredWorkModel):
    """ "Remind me about this thread at 4pm" (SPEC §14).

    Fires as an in-app notification rather than a message: the recipient is a
    team member, the notification engine (issue #7) already carries the bell,
    the deep link and the per-person email preference, and
    ``apps/notifications/events.py`` registered ``inbox_reminder`` for this.
    """

    conversation = models.ForeignKey(
        "messaging.Conversation",
        on_delete=models.CASCADE,
        related_name="reminders",
    )
    #: Who is reminded. Not necessarily who set it — SPEC §14 says "remind
    #: me/member", and handing a thread to a colleague at a time is the whole
    #: point of the second half.
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="inbox_reminders",
    )
    note = models.CharField(max_length=MAX_REMINDER_NOTE_CHARS, blank=True, default="")
    remind_at = models.DateTimeField()

    class Meta:
        db_table = "inbox_reminder"
        ordering = ["remind_at"]
        indexes = [
            models.Index(fields=["workspace", "conversation", "status"], name="reminder_ws_conv_status_idx"),
        ]

    def __str__(self) -> str:
        return f"reminder for {self.recipient_id} at {self.remind_at:%Y-%m-%d %H:%M}"


class ScheduledReply(DeferredWorkModel):
    """Compose now, send later (SPEC §14).

    ``body`` is exactly what ``OutboundMessage.to_body()`` produces and
    ``apps.messaging.rendering.outbound_from_body`` reads back — the shape
    ``Message.body`` already calls "a persisted contract". Storing anything else
    would be a second serialisation of the same thing, and this one carries media
    blocks for free.

    **Compliance is re-decided when it fires, never at compose time.** A window
    can close in the hours between; the send goes through
    ``apps.messaging.services.send_as_agent``, which is the chokepoint, and a
    refusal comes back as a failed ``Message`` that this row records and
    surfaces. Nothing is silently dropped.
    """

    conversation = models.ForeignKey(
        "messaging.Conversation",
        on_delete=models.CASCADE,
        related_name="scheduled_replies",
    )
    body = models.JSONField(default=dict, blank=True)
    send_at = models.DateTimeField()
    #: The row the send produced, whatever its status. Present on a failure too:
    #: the failed message is what carries the provider's machine-readable code
    #: into the thread, and the operator needs to see both halves.
    message = models.ForeignKey(
        "messaging.Message",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    #: The machine-readable code from a refused send, rendered through
    #: ``apps.messaging.codes.describe``. Never a provider sentence: those quote
    #: the request that caused them, credentials included (SECURITY-BASELINE §5).
    error = models.CharField(max_length=200, blank=True, default="")

    class Meta:
        db_table = "inbox_scheduled_reply"
        ordering = ["send_at"]
        indexes = [
            models.Index(fields=["workspace", "conversation", "status"], name="schedreply_ws_conv_status_idx"),
        ]

    def __str__(self) -> str:
        return f"scheduled reply at {self.send_at:%Y-%m-%d %H:%M}"
