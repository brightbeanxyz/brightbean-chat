"""Broadcasts and their recipients — SPEC §5's ``broadcast``, SPEC §13.

Two tables. :class:`Broadcast` is the thing an operator composes; SPEC §5 fixes
its columns and this module adds nothing to them that is not bookkeeping.
:class:`BroadcastRecipient` is one row per targeted contact, and it is the part
worth explaining because SPEC only asks for a ``stats`` json.

--------------------------------------------------------------------------
Why a recipient row rather than counters on the broadcast
--------------------------------------------------------------------------

SPEC §13.2 wants live counters "from stats json (updated in batches)". The
tempting implementation — every ``broadcast_send`` handler incrementing a
counter on the broadcast row — does not survive contact with the worker:
``apps.queueing.worker.process_action`` wraps each handler in one
``transaction.atomic()``, so a row-level lock taken inside it is **held across
the provider call**. Ten thousand sends would then serialise behind one row for
the length of ten thousand HTTP round trips.

A recipient row is the send's own row, so there is no contention at all, and
three other things fall out of it:

* the acceptance criterion ``queued = sent + failed + cancelled + skipped``
  reconciles by construction, because every term is a count of these rows;
* ``unique (broadcast, contact)`` makes fanout re-entrant independently of the
  queue's idempotency key, which is what a forced worker retry needs (SPEC §21);
* ``message`` gives delivered/read for free from the column
  ``apps.messaging.ingest`` already advances when a delivery receipt arrives —
  so this app needs no second receipt path and no edit to ``apps.messaging``.

--------------------------------------------------------------------------
Content is a flow **or** a template, never both
--------------------------------------------------------------------------

SPEC §5: "message flow_id OR whatsapp_template_id". The mini-flow is a real
``flows.Flow`` carrying a single-node graph, kept ``archived`` so it stays out of
the flow list, and the version is **pinned** on the broadcast when it is
scheduled — an edit to the graph mid-send must not change what the rest of the
audience receives. A check constraint holds the exclusive-or rather than a
service remembering it.
"""

from typing import Any

from django.conf import settings
from django.db import models

from apps.common.scoping import WorkspaceScopedModel

__all__ = [
    "Broadcast",
    "BroadcastRecipient",
    "BroadcastStatus",
    "LIVE_STATUSES",
    "RecipientStatus",
    "TERMINAL_RECIPIENT_STATUSES",
]


class BroadcastStatus(models.TextChoices):
    """SPEC §5's five, and the transitions between them.

    ``draft`` → ``scheduled`` → ``sending`` → ``sent``, with ``cancelled``
    reachable from ``scheduled`` and ``sending`` only. A ``sent`` broadcast is
    never re-opened: the messages are gone.
    """

    DRAFT = "draft", "Draft"
    SCHEDULED = "scheduled", "Scheduled"
    SENDING = "sending", "Sending"
    SENT = "sent", "Sent"
    CANCELLED = "cancelled", "Cancelled"


#: Statuses from which a broadcast still has work in the queue, and therefore the
#: only ones :func:`apps.broadcasts.services.cancel_broadcast` accepts.
LIVE_STATUSES: frozenset[str] = frozenset({BroadcastStatus.SCHEDULED, BroadcastStatus.SENDING})


class RecipientStatus(models.TextChoices):
    """What happened to one person's copy.

    ``skipped`` is a compliance verdict — the eligibility filter or the send-time
    re-check refused — and carries the :class:`apps.messaging.codes.Denial` code
    in ``reason``. ``failed`` is a send that was attempted and did not land.
    Keeping them apart is what makes SPEC §13.2's ``skipped_window`` countable
    separately from a provider error.
    """

    PENDING = "pending", "Pending"
    SENT = "sent", "Sent"
    FAILED = "failed", "Failed"
    SKIPPED = "skipped", "Skipped"
    CANCELLED = "cancelled", "Cancelled"


#: Statuses a recipient never leaves. Everything else is still owed a send, which
#: is how "is this broadcast finished?" is asked.
TERMINAL_RECIPIENT_STATUSES: frozenset[str] = frozenset(
    {RecipientStatus.SENT, RecipientStatus.FAILED, RecipientStatus.SKIPPED, RecipientStatus.CANCELLED}
)


class Broadcast(WorkspaceScopedModel):
    """One-to-many send: an audience, a message, and a schedule (SPEC §13)."""

    name = models.CharField(max_length=200)
    channel_connection = models.ForeignKey(
        "channels.ChannelConnection",
        on_delete=models.CASCADE,
        related_name="broadcasts",
    )

    # The condition document (contract 8's schema), authoritative on its own. A
    # broadcast built from a saved segment copies the segment's filter here and
    # keeps `segment` for provenance only: a segment edited after a broadcast was
    # scheduled must not change who the broadcast goes to.
    target_filter_json = models.JSONField(default=dict, blank=True)
    segment = models.ForeignKey(
        "contacts.Segment",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="broadcasts",
        help_text="Where target_filter_json came from. Provenance only; the document is authoritative.",
    )

    # -- content: exactly one of these two, per SPEC §5 ----------------------
    flow = models.ForeignKey(
        "flows.Flow",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="broadcasts",
        help_text="The mini-flow holding this broadcast's single-node graph.",
    )
    flow_version = models.ForeignKey(
        "flows.FlowVersion",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="broadcasts",
        help_text="Pinned when the broadcast is scheduled, so an edit mid-send changes nothing.",
    )
    whatsapp_template = models.ForeignKey(
        "channels.WhatsAppTemplate",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="broadcasts",
    )
    #: ``{"body.1": "Ada"}`` — slot name to value, the shape
    #: ``apps.channels.whatsapp_templates.variable_schema`` describes.
    template_variables = models.JSONField(default=dict, blank=True)

    #: SPEC §6.4's non-promotional message tag. Blank rather than null: a
    #: CharField with two ways to say "no tag" makes every read handle both.
    #: Validated against ``PlatformPolicy.outside_window.tags`` on the way in and
    #: again by the compliance engine on the way out.
    message_tag = models.CharField(max_length=64, blank=True, default="")

    scheduled_at = models.DateTimeField(null=True, blank=True, help_text="Null means send as soon as it is started.")
    status = models.CharField(max_length=16, choices=BroadcastStatus.choices, default=BroadcastStatus.DRAFT)

    #: SPEC §5's counters: queued, sent, delivered, failed, skipped_window and
    #: the rest of the per-reason skips. Materialised from the recipient rows by
    #: :func:`apps.broadcasts.services.counters`, written back only when it
    #: changed — SPEC §13.2's "updated in batches".
    stats = models.JSONField(default=dict, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "broadcasts_broadcast"
        ordering = ["-created_at"]
        constraints = [
            # SPEC §5's "flow_id OR whatsapp_template_id". Held by the database
            # because both send paths branch on it, and a row with both set would
            # make that branch arbitrary.
            models.CheckConstraint(
                condition=models.Q(flow__isnull=True) | models.Q(whatsapp_template__isnull=True),
                name="broadcast_content_is_flow_or_template",
            ),
        ]
        indexes = [
            models.Index(fields=["workspace", "status"], name="broadcast_ws_status_idx"),
            # The scheduler's read: what is due to start.
            models.Index(fields=["status", "scheduled_at"], name="broadcast_status_due_idx"),
        ]

    def __str__(self) -> str:
        return self.name

    @property
    def is_live(self) -> bool:
        """Whether the queue still holds work for this broadcast."""
        return self.status in LIVE_STATUSES

    @property
    def platform(self) -> str:
        return str(self.channel_connection.platform)


class BroadcastRecipient(WorkspaceScopedModel):
    """One person's copy of one broadcast. See the module docstring."""

    broadcast = models.ForeignKey(Broadcast, on_delete=models.CASCADE, related_name="recipients")
    #: ``SET_NULL`` rather than ``CASCADE``, and nullable, because of issue
    #: #29's GDPR erasure. This row is a **counter**: :func:`counters` recomputes
    #: a finished broadcast's figures from these rows live, while the list page
    #: reads the ``stats`` json ``settle()`` froze. A cascade would delete the
    #: counter along with the person and leave the two disagreeing about a
    #: broadcast that has already been sent. Nulling the three references
    #: instead leaves a row carrying a status and a machine-readable ``reason``
    #: and nothing else — SPEC §19's "keep anonymized counters", exactly.
    #:
    #: The ``(broadcast, contact)`` unique constraint still holds: Postgres
    #: treats NULLs as distinct, which is the right reading here — two erased
    #: recipients are two recipients, not one recorded twice.
    contact = models.ForeignKey(
        "contacts.Contact",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="broadcast_recipients",
    )
    identity = models.ForeignKey(
        "messaging.ContactChannelIdentity",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="broadcast_recipients",
    )
    status = models.CharField(max_length=16, choices=RecipientStatus.choices, default=RecipientStatus.PENDING)

    #: A machine-readable code from :mod:`apps.messaging.codes`, never a
    #: sentence: the copy a person reads is looked up with ``codes.describe`` at
    #: render time, the same rule ``Message.error`` follows (SECURITY-BASELINE §5).
    reason = models.CharField(max_length=200, blank=True, default="")

    message = models.ForeignKey(
        "messaging.Message",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text="The row a delivery receipt advances; how delivered/read counters stay live.",
    )

    class Meta:
        db_table = "broadcasts_recipient"
        constraints = [
            # One send per contact per broadcast. Independent of the queue's
            # idempotency key on purpose: this is what makes a re-run of the
            # fanout handler — which zombie recovery can force — insert nothing.
            models.UniqueConstraint(fields=["broadcast", "contact"], name="broadcast_recipient_unique"),
        ]
        indexes = [
            # The counter query's exact shape: GROUP BY status for one broadcast.
            models.Index(fields=["broadcast", "status"], name="recipient_broadcast_status_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.contact_id} ({self.status})"

    @property
    def reason_label(self) -> str:
        """The sentence for this row's code, looked up rather than stored.

        ``apps.messaging.codes.describe`` is the registered copy the inbox
        already shows for the same code, so a broadcast and a failed message
        explain themselves to an operator in the same words — and a code a later
        platform introduces explains itself here with no edit.
        """
        from apps.messaging.codes import describe

        return describe(self.reason)

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Derive ``workspace`` from the broadcast, the way ``Message`` does.

        Same ``update_fields`` caveat as ``apps.messaging.models.Message.save``:
        Django reads a *falsy* ``update_fields`` as "save nothing", so widening
        an empty one would turn a documented no-op into a real UPDATE.

        Not covered: ``bulk_create``, which bypasses ``save()`` — and fanout does
        bulk-create these five hundred at a time. It sets ``workspace`` on every
        instance itself, which is why this is a convenience for the handful of
        single saves rather than the invariant's only guard.
        """
        if self.workspace_id is None and self.broadcast_id is not None:
            self.workspace_id = self.broadcast.workspace_id
        update_fields = kwargs.get("update_fields")
        if update_fields:
            kwargs["update_fields"] = set(update_fields) | {"workspace"}
        super().save(*args, **kwargs)
