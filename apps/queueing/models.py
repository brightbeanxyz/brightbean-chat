"""The scheduled-action table: one row per unit of deferred work (SPEC §5, §15).

This is the whole scheduling substrate. SPEC §22 is absolute — no Redis, ever —
so every time-based behaviour in the product (flow delays, send retries,
sequence steps, broadcast fanout, followup timers, outbound webhook deliveries,
housekeeping) is a row here that a worker claims, runs and marks terminal.

Two shapes of row live in this table and the difference is ``workspace``:

* **Tenant work** carries a workspace, like every other tenant table.
* **Deployment work** — the hourly ``housekeeping`` chain — carries ``NULL``.

That is why ``workspace`` is redeclared nullable below, and it is worth being
precise about why the nullable column is the *safe* option rather than a
loophole. ``WorkspaceScopedQuerySet.for_workspace()`` filters on a concrete id,
so a NULL-workspace row matches no tenant query that has ever been written: a
system row is reachable only from code that says ``.unscoped()`` out loud. The
alternative — a sentinel "System" workspace row — would put a fake tenant into
``apps.workspaces``, the workspace switcher and every listing that forgets to
filter it out.
"""

from typing import Any
from uuid import UUID

from django.db import models

from apps.common.scoping import WorkspaceScopedModel


def coerce_contact_id(contact: Any) -> UUID | str | None:
    """Normalise a ``Contact``, a UUID or a string to what ``contact_id`` holds.

    One implementation, deliberately, and it lives next to the column it feeds.
    Two callers depend on it agreeing with itself: :mod:`apps.queueing.registry`
    writes the value into the column, and :mod:`apps.queueing.locks` hashes the
    same value into the advisory-lock key the whole engine serialises on
    (SPEC §9.6). If those two ever normalised differently — one keeping
    ``"AB-…"``, the other lowercasing it — the worker would lock a key nobody
    else computes, and the one-step-per-contact invariant would fail silently
    under exactly the concurrency it exists for.

    Returns a canonical ``UUID`` where the input parses as one, so a freshly
    created row and one read back from the database compare equal. A
    non-UUID string is passed through rather than coerced, and left for the
    column to reject on save.
    """
    if contact is None:
        return None
    value = getattr(contact, "pk", contact)
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return str(value)


class ActionType(models.TextChoices):
    """The action types SPEC §5 names. **Not a closed set.**

    Deliberately not attached to the model field as ``choices=``. The authority
    on what may be processed is the handler registry
    (:mod:`apps.queueing.registry`), which later layers append to: L2-B
    registers webhook-log pruning, L2-E email delivery, L3-B resume/start,
    L5-F ``webhook_delivery``, L6-C ``reminder`` and ``scheduled_reply``. If the
    column carried ``choices`` every one of those would need an ``AlterField``
    migration *in this app*, which is exactly the coupling the issue's "leave
    the enum open" instruction exists to prevent.

    These constants are here so call sites spell the common types the same way.
    """

    RESUME_EXECUTION = "resume_execution", "Resume execution"
    START_FLOW = "start_flow", "Start flow"
    SEQUENCE_STEP = "sequence_step", "Sequence step"
    BROADCAST_FANOUT = "broadcast_fanout", "Broadcast fanout"
    BROADCAST_SEND = "broadcast_send", "Broadcast send"
    SEND_RETRY = "send_retry", "Send retry"
    FOLLOWUP_TIMER = "followup_timer", "Followup timer"
    HOUSEKEEPING = "housekeeping", "Housekeeping"


class ActionStatus(models.TextChoices):
    """The lifecycle. Closed, unlike :class:`ActionType`.

    ``pending`` → ``running`` (claimed) → ``done`` | ``failed``, with
    ``running`` → ``pending`` on a retriable error and on zombie recovery.
    ``cancelled`` is set by owners of the work (a cancelled broadcast, a
    superseded execution), never by the worker.
    """

    PENDING = "pending", "Pending"
    RUNNING = "running", "Running"
    DONE = "done", "Done"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"


#: Statuses a row will never leave on its own.
TERMINAL_STATUSES = frozenset({ActionStatus.DONE, ActionStatus.FAILED, ActionStatus.CANCELLED})

#: Default retry budget (SPEC §5). See ``apps.queueing.worker.BACKOFF_SCHEDULE``
#: for what the attempts are spaced by.
DEFAULT_MAX_ATTEMPTS = 5


class ScheduledAction(WorkspaceScopedModel):
    """One unit of deferred work."""

    # Redeclared from WorkspaceScopedModel to allow NULL. Django's "field name
    # hiding is not permitted" rule exempts fields inherited from an *abstract*
    # base, which is what this is. See the module docstring for why NULL is the
    # safe representation of a deployment-level job.
    #
    # django-stubs types an inherited field as non-optional and has no way to
    # express the narrowing, so the ignore is the override itself rather than a
    # papered-over bug; the migration and the tests both prove Django accepts it.
    workspace = models.ForeignKey(  # type: ignore[assignment]
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="scheduled_actions",
        null=True,
        blank=True,
        help_text="NULL for deployment-level system jobs (housekeeping). No tenant query can see those.",
    )

    # A plain UUID column, NOT a ForeignKey to apps.contacts.Contact.
    #
    # Two reasons, in order of weight. (1) apps.contacts is issue #3, a
    # same-layer sibling; ROADMAP forbids depending on one outside the written
    # contracts, and a hard FK is about as hard a dependency as there is.
    # (2) Even once contacts lands, the queue is deliberately generic
    # substrate — it schedules work for broadcasts, sequences and housekeeping
    # that no contact owns, and it should not grow an import of every domain it
    # schedules for. The column exists so the worker knows which advisory lock
    # to take (SPEC §9.6) and so (contact_id, status) can be indexed.
    contact_id = models.UUIDField(null=True, blank=True)

    run_at = models.DateTimeField(help_text="Earliest moment a worker may claim this row.")
    type = models.CharField(max_length=64, help_text="Handler key; see apps.queueing.registry.")
    payload = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=16, choices=ActionStatus.choices, default=ActionStatus.PENDING)
    attempts = models.PositiveIntegerField(default=0)
    max_attempts = models.PositiveIntegerField(default=DEFAULT_MAX_ATTEMPTS)
    last_error = models.TextField(blank=True, default="")

    # Nullable rather than blank-defaulted, because NULLs are distinct in a
    # Postgres unique index and "" is not: a second key-less row would collide
    # with the first. NULL here means "no idempotency guarantee wanted", which
    # is the common case.
    idempotency_key = models.CharField(max_length=255, null=True, blank=True, unique=True)

    class Meta:
        db_table = "queueing_scheduled_action"
        ordering = ["run_at"]
        indexes = [
            # The claim query's exact shape: WHERE status='pending' AND
            # run_at <= now() ORDER BY run_at.
            models.Index(fields=["status", "run_at"], name="scheduledaction_status_run_idx"),
            models.Index(fields=["contact_id", "status"], name="scheduledaction_contact_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.type} @ {self.run_at:%Y-%m-%d %H:%M:%S} ({self.status})"

    @property
    def is_system(self) -> bool:
        """True for deployment-level work that belongs to no tenant."""
        return self.workspace_id is None
