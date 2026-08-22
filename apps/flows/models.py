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
from apps.contacts.models import ContactScopedModel
from apps.flows.schema import empty_graph

__all__ = [
    "LIVE_STATUSES",
    "ExecutionStatus",
    "Flow",
    "FlowExecution",
    "FlowStatus",
    "FlowVersion",
    "StartedBy",
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
