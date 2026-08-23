"""Flows, their versions, and the executions that run them (SPEC §5, "flows").

Two tenant tables and one invariant worth stating up front: **a flow has at most
one published version.** That is a partial unique index here, not a rule
services remember, because it is what every other layer assumes — the engine
starts "the published version", triggers fire "the published version", and a
second published row would make that phrase ambiguous at the worst moment.

Versioning shape, from the issue: editing writes or updates the *latest draft*,
and publishing flips flags rather than copying a graph. So the newest row is the
draft the builder is editing, unless it is published, in which case the next
edit opens a new one. :mod:`apps.flows.services` owns those transitions —
nothing outside it should be setting ``published`` by hand.

``FlowVersion`` carries its own ``workspace`` foreign key rather than reaching
through ``flow``. SPEC §5 requires one on every tenant table, and it means
``get_scoped_object_or_404(FlowVersion, workspace, ...)`` works directly instead
of via a join that a future query might forget.

``FlowExecution`` is the engine's durable state machine (SPEC §9.2). The
behaviour that reads and writes it lives in :mod:`apps.flows.engine`; what is
here is the table, the status vocabulary, and the two invariants the database
itself holds — one live execution per (contact, flow), and an execution whose
contact, flow and workspace cannot disagree.
"""

from typing import Any

from django.conf import settings
from django.db import models

from apps.common.scoping import WorkspaceScopedModel
from apps.contacts.errors import WorkspaceMismatchError
from apps.contacts.models import ContactScopedModel
from apps.flows.schema import empty_graph
from apps.flows.triggers.types import TriggerType

__all__ = [
    "LIVE_STATUSES",
    "DefaultReplyState",
    "ExecutionStatus",
    "Flow",
    "FlowExecution",
    "FlowStatus",
    "FlowVersion",
    "HandledComment",
    "RoutedEvent",
    "StartedBy",
    "Trigger",
    "TriggerType",
]


class FlowStatus(models.TextChoices):
    """SPEC §5. ``active`` is set by publishing, never by hand."""

    DRAFT = "draft", "Draft"
    ACTIVE = "active", "Active"
    ARCHIVED = "archived", "Archived"


class Flow(WorkspaceScopedModel):
    """One automation. Its runnable content lives in its versions."""

    name = models.CharField(max_length=200)
    status = models.CharField(max_length=16, choices=FlowStatus.choices, default=FlowStatus.DRAFT)

    # SPEC §5 says "nullable text". It is an empty string here instead: a
    # nullable CharField gives two different ways to say "no folder", and every
    # grouping query then has to handle both or quietly drop rows. "" is the
    # only "unfiled" there is.
    folder = models.CharField(max_length=200, blank=True, default="")

    class Meta:
        # Not Meta inheritance: WorkspaceScopedModel deliberately declares its
        # managers as class attributes so a subclass writing its own Meta cannot
        # drop them (see apps/common/scoping.py).
        indexes = [
            models.Index(fields=["workspace", "status"], name="flows_flow_ws_status_idx"),
            models.Index(fields=["workspace", "folder"], name="flows_flow_ws_folder_idx"),
        ]
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class FlowVersion(WorkspaceScopedModel):
    """One revision of a flow's graph. Monotonic per flow; at most one published."""

    flow = models.ForeignKey(Flow, on_delete=models.CASCADE, related_name="versions")
    version = models.PositiveIntegerField()
    graph_json = models.JSONField(default=empty_graph)
    published = models.BooleanField(default=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        # Nothing reads "versions this user created", and a reverse accessor
        # nothing uses is a name every future User relation has to avoid.
        related_name="+",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["flow", "version"], name="flows_version_unique_per_flow"),
            # The partial index the issue asks for. Concurrent publishes are
            # already serialised on the flow row in services.publish(); this is
            # the database refusing to hold a state no code should produce, in
            # case some future path forgets the lock.
            models.UniqueConstraint(
                fields=["flow"],
                condition=models.Q(published=True),
                name="flows_one_published_version_per_flow",
            ),
        ]
        indexes = [models.Index(fields=["flow", "-version"], name="flows_version_latest_idx")]
        ordering = ["-version"]

    def __str__(self) -> str:
        return f"{self.flow_id} v{self.version}"

    @property
    def is_draft(self) -> bool:
        return not self.published

    def as_dict(self) -> dict[str, Any]:
        """Version metadata for the builder. The graph is sent separately."""
        return {
            "id": str(self.pk),
            "version": self.version,
            "published": self.published,
            "updated_at": self.updated_at.isoformat(),
        }


class ExecutionStatus(models.TextChoices):
    """SPEC §5's ``flow_execution.status``.

    Three of the six are *live* — the execution is somewhere in the middle of a
    graph and will move again. The other three are terminal and nothing but
    housekeeping ever writes them a second time.
    """

    RUNNING = "running", "Running"
    WAITING_REPLY = "waiting_reply", "Waiting for a reply"
    WAITING_DELAY = "waiting_delay", "Waiting for a delay"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"
    EXPIRED = "expired", "Expired"


#: The statuses that mean "this execution still owns the contact".
#:
#: SPEC §9.2 and §22: exactly one live execution per contact, across every flow.
#: The partial unique index below holds the per-flow half of that; the
#: cross-flow half is :func:`apps.flows.engine.start_flow`, which expires the
#: contact's live executions under the contact advisory lock before creating a
#: new one. Everything that asks "is this contact busy?" asks with this set.
LIVE_STATUSES: frozenset[str] = frozenset(
    {ExecutionStatus.RUNNING, ExecutionStatus.WAITING_REPLY, ExecutionStatus.WAITING_DELAY}
)


class StartedBy:
    """The vocabulary of ``flow_execution.started_by`` (SPEC §5).

    SPEC gives the column one value covering "trigger id / broadcast id /
    sequence id / api", so the kinds live here as constants and the value is
    written ``kind`` or ``kind:id`` — greppable, and parseable without a second
    column. :func:`stamp` is the only thing that builds one.
    """

    TRIGGER = "trigger"
    BROADCAST = "broadcast"
    SEQUENCE = "sequence"
    API = "api"
    MANUAL = "manual"
    #: A draft-version run started from the builder (#12's "test on Telegram").
    PREVIEW = "preview"
    #: SPEC §11.3's start_flow node, naming the execution that handed over.
    FLOW = "flow"

    KINDS: tuple[str, ...] = (TRIGGER, BROADCAST, SEQUENCE, API, MANUAL, PREVIEW, FLOW)

    @classmethod
    def stamp(cls, kind: str, identifier: Any = None) -> str:
        """``"trigger:0192…"``, or just ``"api"`` when nothing identifies it."""
        if kind not in cls.KINDS:
            raise ValueError(f"{kind!r} is not a started_by kind; known: {', '.join(cls.KINDS)}.")
        return kind if identifier is None else f"{kind}:{identifier}"


class FlowExecution(ContactScopedModel):
    """One contact's run through one flow version — SPEC §5's ``flow_execution``.

    The durable half of the engine (SPEC §9.2). Everything the runner needs to
    pick a paused run back up hours later is a column here: where it stopped,
    what it is waiting for, what it has collected, and how many blocks it has
    executed since it last paused.

    ``ContactScopedModel`` rather than ``WorkspaceScopedModel`` directly: it
    derives ``workspace`` from the contact and refuses a peer whose workspace
    disagrees, which is exactly the invariant an execution needs — a contact
    from one workspace can never be run through another workspace's flow. The
    peer is ``flow`` rather than ``flow_version`` because the two are pinned to
    each other by ``FlowVersion.flow`` anyway, and ``flow`` is the column the
    partial unique index needs.
    """

    peer_field = "flow"

    # Denormalised from flow_version.flow. SPEC §5 writes the partial unique
    # index over (contact_id, flow_id), and an index cannot reach through a
    # join — so the column has to be here. `save()` keeps the two in step.
    flow = models.ForeignKey(Flow, on_delete=models.CASCADE, related_name="executions")
    flow_version = models.ForeignKey(FlowVersion, on_delete=models.CASCADE, related_name="executions")
    contact = models.ForeignKey("contacts.Contact", on_delete=models.CASCADE, related_name="flow_executions")

    # Not in SPEC §5's column list, and deliberate. ROADMAP contract 1 requires
    # a connection on every send_outbound() call, and SPEC §9.3 routes inbound
    # events to "waiting execution on that channel" — both need the execution to
    # remember which channel it is running on. Nullable because a run started by
    # the API or a rule trigger has no channel until it sends.
    channel_connection = models.ForeignKey(
        "channels.ChannelConnection",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="flow_executions",
    )

    status = models.CharField(max_length=16, choices=ExecutionStatus.choices, default=ExecutionStatus.RUNNING)

    # The node about to run, or — while waiting — the node that is waiting.
    # Blank only for an execution that has run off the end of its graph.
    current_node_id = models.CharField(max_length=64, blank=True, default="")

    variables = models.JSONField(default=dict, blank=True)

    #: SPEC §9.2's loop cap counter. Reset by every Wait and Schedule.
    blocks_since_pause = models.PositiveIntegerField(default=0)

    #: What will resume this execution (SPEC §9.3). Empty while running.
    #: Shapes and the token discipline live in ``apps.flows.engine.waits``.
    wait_config = models.JSONField(default=dict, blank=True)

    started_by = models.CharField(max_length=100, blank=True, default="")

    #: A run of an unpublished draft, started from the builder's preview
    #: (SPEC §16, issue #12). Flagged rather than hidden: it is a real execution
    #: with real sends, and L7-A's per-node counters exclude it so a few test
    #: runs cannot move a flow's reported numbers.
    preview = models.BooleanField(default=False)

    #: Why a failed execution failed. Scrubbed and capped on write.
    last_error = models.TextField(blank=True, default="")

    class Meta:
        db_table = "flows_flow_execution"
        constraints = [
            # SPEC §5, verbatim: "one row per (contact_id, flow_id) where status
            # in (running, waiting_reply, waiting_delay)". Concurrent starts are
            # already serialised on the contact advisory lock (SPEC §9.6); this
            # is the database refusing to hold a state no code should produce.
            models.UniqueConstraint(
                fields=["contact", "flow"],
                condition=models.Q(status__in=sorted(LIVE_STATUSES)),
                name="flows_one_live_execution_per_contact_flow",
            ),
        ]
        indexes = [
            # SPEC §5. The stale-execution sweep reads exactly this shape:
            # status in (waiting_*) AND updated_at < cutoff.
            models.Index(fields=["status", "updated_at"], name="flowexec_status_updated_idx"),
            # "Is this contact busy, and with what?" — the question every start
            # and every inbound event asks first.
            models.Index(fields=["workspace", "contact", "status"], name="flowexec_ws_contact_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.flow_id} · {self.contact_id} ({self.status})"

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Keep ``flow`` in step with ``flow_version`` before the peer check runs.

        The two columns are one fact written twice, so nothing outside this
        model should have to remember to set both — and a caller that set only
        ``flow_version`` would otherwise reach ``ContactScopedModel.save()``
        with no peer at all.
        """
        if self.flow_version_id is not None and self.flow_id != self.flow_version.flow_id:
            self.flow_id = self.flow_version.flow_id
            update_fields = kwargs.get("update_fields")
            if update_fields:
                kwargs["update_fields"] = set(update_fields) | {"flow"}
        super().save(*args, **kwargs)

    @property
    def is_live(self) -> bool:
        """True while this execution still owns its contact (SPEC §9.2)."""
        return self.status in LIVE_STATUSES


class Trigger(WorkspaceScopedModel):
    """What starts a flow — SPEC §5's ``trigger`` row, SPEC §10's behaviour.

    Matching runs in priority order, lower first, and **the first match wins per
    event**. That makes ``Meta.ordering`` load-bearing rather than cosmetic: with
    ``priority`` alone two triggers at the same number tie, and a tie means the
    same message can start different flows on different days depending on how
    Postgres felt about the seq scan. ``created_at`` then ``id`` closes it — uuid7
    is monotonic, so "the one that was there first" is also the stable answer.

    ``channel_connection`` is nullable, and SPEC §5 says what null means: *all
    connections of matching platform*. "Matching" is not a wildcard — it is
    :data:`apps.flows.triggers.types.PLATFORMS_FOR_TYPE`, SPEC §10's Channels
    column as data. A welcome trigger with no connection covers this workspace's
    Telegram and Messenger connections and not its SMS one, because a welcome is
    a thing only those two platforms send.

    There is deliberately **no uniqueness** on (workspace, type, connection).
    Two default replies on one channel are legal and resolved by priority, like
    every other type: SPEC §10 gives one resolution rule for all ten types, and
    carving out an exception would also mean a toggle button that can answer
    ``IntegrityError`` when someone enables a staged replacement before disabling
    the old one. The panel warns about the duplicate instead, which is the part
    the author actually needs to know.
    """

    flow = models.ForeignKey(Flow, on_delete=models.CASCADE, related_name="triggers")

    # CASCADE, not SET_NULL: null already means something specific here ("every
    # connection of a matching platform"), so a deleted connection quietly
    # widening a trigger from one channel to all of them is the one outcome that
    # must not happen.
    channel_connection = models.ForeignKey(
        "channels.ChannelConnection",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="triggers",
    )

    type = models.CharField(max_length=20, choices=TriggerType.choices)
    config_json = models.JSONField(default=dict, blank=True)
    enabled = models.BooleanField(default=True)
    priority = models.IntegerField(default=0)

    class Meta:
        db_table = "flows_trigger"
        ordering = ["priority", "created_at", "id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(priority__gte=0),
                name="flows_trigger_priority_non_negative",
            ),
        ]
        indexes = [
            # SPEC §5, verbatim: "Index (workspace_id, type, enabled)". It is the
            # matcher's candidate query, which runs once per inbound event.
            models.Index(fields=["workspace", "type", "enabled"], name="flows_trigger_ws_type_idx"),
            # The panel's query: this flow's triggers, in the order they match.
            models.Index(fields=["flow", "priority"], name="flows_trigger_flow_prio_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.get_type_display()} → {self.flow_id}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Derive ``workspace`` from the flow, and refuse a peer from elsewhere.

        Same discipline as :class:`apps.contacts.models.ContactScopedModel`,
        which this cannot inherit because a trigger hangs off a *flow*, not a
        contact. The connection check is the one that matters: a trigger is the
        join between a flow and a channel, and the two disagreeing about their
        workspace is how one tenant's message starts another tenant's flow.
        """
        self.workspace_id = self.flow.workspace_id
        connection = self.channel_connection if self.channel_connection_id is not None else None
        if connection is not None and connection.workspace_id != self.flow.workspace_id:
            raise WorkspaceMismatchError("That channel connection belongs to a different workspace than the flow.")
        update_fields = kwargs.get("update_fields")
        if update_fields:
            # Only widen a non-empty set — an empty one means "save nothing" and
            # Django returns before touching the database.
            kwargs["update_fields"] = {*update_fields, "workspace"}
        super().save(*args, **kwargs)

    @property
    def covers_all_connections(self) -> bool:
        """Whether this trigger applies to every connection of a matching platform."""
        return self.channel_connection_id is None


class HandledComment(WorkspaceScopedModel):
    """One comment this workspace has already acted on (SPEC §10, comment trigger).

    Two rules live here, and they are different rules with different keys.

    **Idempotency**, on ``(connection, comment_id)``: Meta redelivers webhooks,
    and without this a redelivery is a second private reply to the same person
    about the same comment.

    **Once per contact per post**, on ``(connection, post_id, commenter_ref)``
    and *partial* on the flag: SPEC §10 makes ``once_per_contact_per_post`` a
    per-trigger setting defaulting to true, so rows written while it was off must
    stay out of the index. Storing the flag on the row rather than reading the
    trigger's config at query time also means turning the setting off later does
    not retroactively unlock history.

    The guard key is ``commenter_ref`` — the platform's user id — and **not**
    ``contact``. That is the whole reason this is workspace-scoped rather than
    contact-scoped: ``apps/messaging/ingest.py`` deliberately creates no contact
    for a comment event, so ``contact`` is NULL at the moment the guard has to be
    taken, and Postgres treats NULLs as distinct — a unique constraint over a
    NULL column would never fire for exactly the concurrent race this table
    exists to lose gracefully. The contact is filled in later, when the private
    reply creates the DM identity.
    """

    channel_connection = models.ForeignKey(
        "channels.ChannelConnection",
        on_delete=models.CASCADE,
        related_name="handled_comments",
    )
    trigger = models.ForeignKey(
        Trigger,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="handled_comments",
    )

    #: Platform ids. Attacker-controlled text: escape on render, never trust the
    #: length, and note they are bounded on write rather than truncated.
    comment_id = models.CharField(max_length=200)
    post_id = models.CharField(max_length=200)
    commenter_ref = models.CharField(max_length=200)

    contact = models.ForeignKey(
        "contacts.Contact",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="handled_comments",
    )

    #: When the platform says the comment was written, clamped to "not in the
    #: future" on write. SPEC §10's private reply has a 7-day deadline measured
    #: from here, and a forged timestamp must not be able to buy extra days.
    commented_at = models.DateTimeField()

    #: The trigger's setting at the moment this row was written. See the class
    #: docstring: it is what the partial unique index is conditioned on.
    once_per_contact_per_post = models.BooleanField(default=True)

    private_reply_sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "flows_handled_comment"
        ordering = ["-commented_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["channel_connection", "comment_id"],
                name="flows_handledcomment_unique_comment",
            ),
            models.UniqueConstraint(
                fields=["channel_connection", "post_id", "commenter_ref"],
                condition=models.Q(once_per_contact_per_post=True),
                name="flows_handledcomment_once_per_commenter_per_post",
            ),
        ]
        indexes = [
            models.Index(fields=["workspace", "post_id"], name="flows_hcomment_ws_post_idx"),
            # The housekeeping sweep: rows whose deadline has passed.
            models.Index(fields=["commented_at"], name="flows_hcomment_commented_idx"),
            # "Is this person's next message a private reply?" — asked by an
            # adapter on the send path, which SPEC §7.1 budgets at 1.5 s of wall
            # clock including the outbound call, so it has to be an index lookup
            # rather than a scan. **Partial**, on the unanswered rows only: the
            # answer is no for every row this table keeps after its reply went
            # out, so indexing them would grow the index for ever to answer a
            # question none of them can answer yes to. Added by #17 (L5-A).
            models.Index(
                fields=["channel_connection", "commenter_ref"],
                condition=models.Q(private_reply_sent_at__isnull=True),
                name="flows_hcomment_pending_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"comment {self.comment_id} on post {self.post_id}"


class DefaultReplyState(WorkspaceScopedModel):
    """SPEC §10's fixed 24-hour default-reply guard, per contact per channel.

    Deliberately a table rather than :func:`apps.common.ratelimit.hit`, on three
    counts. That limiter's window is **clock-aligned**, so a default reply at
    23:59:59 and another at 00:00:01 both pass — which is precisely the "two
    identical fallbacks seconds apart" complaint the guard exists to prevent. Its
    key is a SHA-256 digest, so an operator asking "why did this person get two"
    has nothing readable to look at. And ``hit()`` prunes the whole counter table
    on every new key, on the inbound path, inside SPEC §7.1's 1.5-second budget.

    A row per (contact, connection) with a rolling ``last_sent_at`` is none of
    those things, and it is what "durable" in the issue asks for.

    Workspace-scoped rather than contact-scoped for one reason: the claim below
    is a single ``UPDATE ... WHERE``, and ``ContactScopedModel.save()`` exists to
    police the ``save()`` path, which the claim does not use on its hot leg.
    ``contact`` and ``channel_connection`` are checked against the workspace by
    the service that writes them.
    """

    contact = models.ForeignKey(
        "contacts.Contact",
        on_delete=models.CASCADE,
        related_name="default_reply_states",
    )
    channel_connection = models.ForeignKey(
        "channels.ChannelConnection",
        on_delete=models.CASCADE,
        related_name="default_reply_states",
    )
    last_sent_at = models.DateTimeField()

    class Meta:
        db_table = "flows_default_reply_state"
        ordering = ["-last_sent_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["contact", "channel_connection"],
                name="flows_defaultreply_one_per_contact_channel",
            ),
        ]
        indexes = [models.Index(fields=["last_sent_at"], name="flows_defaultreply_sent_idx")]

    def __str__(self) -> str:
        return f"default reply to {self.contact_id} at {self.last_sent_at:%Y-%m-%d %H:%M}"


class RoutedEvent(WorkspaceScopedModel):
    """One inbound event the **worker** has finished routing. Exactly-once, durably.

    Only the deferred path writes here, and the reason is specific enough to be
    worth stating. ``apps.queueing.worker.process_action`` runs a handler and
    marks the row done in one transaction, so a crash rolls both back and the
    retry is honest. But zombie recovery resets a ``running`` row after ten
    minutes, so a genuinely slow handler can be claimed a second time while the
    first is still in flight. Both take the blocking contact lock, so they
    serialise — and the second then re-runs the pipeline over the first's
    committed work, calling ``start_flow`` again.

    ``send_outbound``'s idempotency key does not save us there: it is
    ``exec:{execution_id}:node:...``, and a second ``start_flow`` mints a *new*
    execution id, so the second welcome message is a legitimately different send.
    SPEC §21 asks for zero duplicate sends across a thousand forced retries.

    So the handler's first act is this insert, and it commits with the work:
    rolled back together when the handler fails (a retry re-runs, correctly),
    committed together when it succeeds (a zombie re-run is a no-op). The inline
    path is single-shot by construction — ``webhook_event_log`` has already
    deduplicated the delivery — and does not pay a write per event.
    """

    channel_connection = models.ForeignKey(
        "channels.ChannelConnection",
        on_delete=models.CASCADE,
        related_name="routed_events",
    )
    #: The platform's event id, bounded the way ``messaging.identities`` bounds
    #: keys: hashed when over-long, never truncated, because two different long
    #: ids sharing a prefix would collapse into one another's guard.
    provider_event_id = models.CharField(max_length=200)
    #: The stage the deferred run resumed from, so a hand-off at ``trigger`` and
    #: one at ``resume`` are distinguishable in an operator's read of the table.
    stage = models.CharField(max_length=32)
    #: What routing did with it, for the same reason. Free text, ours, bounded.
    outcome = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        db_table = "flows_routed_event"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["channel_connection", "provider_event_id"],
                name="flows_routedevent_unique_conn_event",
            ),
        ]
        indexes = [models.Index(fields=["workspace", "created_at"], name="flows_routedevent_ws_idx")]

    def __str__(self) -> str:
        return f"{self.provider_event_id} ({self.stage})"
