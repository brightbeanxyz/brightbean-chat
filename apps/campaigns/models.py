"""Sequences, their steps and their enrollments — SPEC §5 (campaigns), §12.

A sequence is a drip campaign: an ordered list of steps, each of which waits a
delay and then **starts a flow**. It owns no message content of its own, and
that is the design rather than an omission — one send path (a flow's
``send_message`` node, through ``apps.messaging.services``) means one place
where compliance, rate limiting and rendering happen. A step that wanted to send
directly would be a second write site, and
``apps/messaging/tests/test_write_sites.py`` fails the build on one.

Four tables:

``Sequence``
    The campaign. Exposes ``name`` and ``.objects.for_workspace(...)``, which is
    the whole contract ``apps/flows/picklists.py::_sequences`` needs.
``SequenceStep``
    ``position``, a delay, a send window and the flow to start.
``SequenceEnrollment``
    One contact's progress. A **partial** unique index allows at most one
    ``active`` row per (sequence, contact) while leaving the history of
    completed and unsubscribed rows intact — SPEC §12's "re-enrollment restarts
    from step 1 (previous enrollment marked unsubscribed)" needs both halves.
``RuleTriggerFire``
    The rule trigger's per-contact-per-trigger cooldown. See
    :mod:`apps.campaigns.rules`.

**Why two of these are** ``ContactScopedModel``. It derives ``workspace`` from
the contact and refuses a peer whose workspace disagrees, which is exactly the
invariant these rows need: an enrollment joins a contact to a sequence, and a
row where those two belong to different tenants is a tenancy bug no test
stumbles over by accident.
"""

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.functions import Lower

from apps.common.scoping import WorkspaceScopedModel
from apps.contacts.models import ContactScopedModel

__all__ = [
    "DEFAULT_SEND_WINDOW",
    "DelayUnit",
    "EnrollmentStatus",
    "RuleTriggerFire",
    "Sequence",
    "SequenceEnrollment",
    "SequenceStatus",
    "SequenceStep",
]


class SequenceStatus(models.TextChoices):
    """SPEC §5's ``sequence.status``.

    Only ``active`` sequences accept new enrollments. Pausing one by moving it
    back to ``draft`` stops new subscribers without disturbing the people
    already part-way through it, which is the behaviour an operator expects from
    a drip campaign they are editing.
    """

    DRAFT = "draft", "Draft"
    ACTIVE = "active", "Active"
    ARCHIVED = "archived", "Archived"


class DelayUnit(models.TextChoices):
    """The units a step's delay is expressed in — SPEC §11.5's set, reused.

    Deliberately the same three the ``smart_delay`` node offers. A sequence step
    and a delay node are the same idea at two scales, and two vocabularies would
    mean two conversions.
    """

    MINUTES = "minutes", "Minutes"
    HOURS = "hours", "Hours"
    DAYS = "days", "Days"


#: A step's window, with sending allowed at any time. The shape is SPEC §11.5's
#: ``continue_window`` exactly, so :func:`apps.common.windows.into_window`
#: serves both without a translation layer.
DEFAULT_SEND_WINDOW: dict[str, object] = {
    "enabled": False,
    "days": [],
    "from": "09:00",
    "to": "17:00",
    "use_contact_timezone": False,
}


class Sequence(WorkspaceScopedModel):
    """A drip campaign: an ordered list of steps a contact walks through."""

    name = models.CharField(max_length=200)
    status = models.CharField(max_length=16, choices=SequenceStatus.choices, default=SequenceStatus.DRAFT)

    class Meta:
        db_table = "campaigns_sequence"
        ordering = ["name"]
        constraints = [
            # Case-insensitive, like `contacts.Tag`: two sequences differing
            # only in case are indistinguishable in a picker, and the flow
            # builder's dropdown is the main way anyone names one.
            models.UniqueConstraint(Lower("name"), "workspace", name="sequence_unique_name_per_workspace"),
        ]

    def __str__(self) -> str:
        return self.name


class SequenceStep(WorkspaceScopedModel):
    """One rung: wait ``delay``, land inside ``send_window``, start ``flow``."""

    sequence = models.ForeignKey(Sequence, on_delete=models.CASCADE, related_name="steps")
    position = models.PositiveIntegerField(help_text="1-based rung number. Contiguous within a sequence.")
    delay_value = models.PositiveIntegerField(default=1)
    delay_unit = models.CharField(max_length=16, choices=DelayUnit.choices, default=DelayUnit.DAYS)
    send_window = models.JSONField(
        default=dict,
        blank=True,
        help_text="SPEC §11.5's shape: {enabled, days[], from, to, use_contact_timezone}.",
    )
    flow = models.ForeignKey("flows.Flow", on_delete=models.PROTECT, related_name="sequence_steps")

    class Meta:
        db_table = "campaigns_sequence_step"
        ordering = ["position"]
        constraints = [
            models.UniqueConstraint(fields=["sequence", "position"], name="step_unique_position_per_sequence"),
        ]

    def __str__(self) -> str:
        return f"{self.sequence_id} step {self.position}"

    def clean(self) -> None:
        """Refuse a step whose flow or sequence belongs to another tenant.

        ``workspace`` here is a denormalisation — the enforcing manager filters
        on it — and a denormalisation is a chance for three columns to disagree.
        ``Trigger.clean()`` refuses an impossible binding for the same reason:
        the services layer checks this too, and a check that only lives there is
        one an admin action or a future importer can walk around.
        """
        super().clean()
        for name in ("sequence", "flow"):
            peer = getattr(self, f"{name}_id", None)
            if peer is not None and getattr(self, name).workspace_id != self.workspace_id:
                raise ValidationError({name: f"That {name} belongs to a different workspace than the step."})

    @property
    def window(self) -> dict:
        """The send window, with the defaults filled in for a partial document."""
        stored = self.send_window if isinstance(self.send_window, dict) else {}
        return {**DEFAULT_SEND_WINDOW, **stored}


class EnrollmentStatus(models.TextChoices):
    """SPEC §5's ``sequence_enrollment.status``."""

    ACTIVE = "active", "Active"
    COMPLETED = "completed", "Completed"
    UNSUBSCRIBED = "unsubscribed", "Unsubscribed"


class SequenceEnrollment(ContactScopedModel):
    """One contact's walk through one sequence.

    ``current_step`` is the **position of the step that runs next**, so a fresh
    enrollment sits at 1 and a finished one keeps the position past the end. It
    is a position rather than a foreign key on purpose: a step deleted mid-flight
    must not cascade an enrollment away, and positions are what the queue payload
    carries.

    ``last_sent_at`` is what the next delay is measured from — SPEC §12's "waits
    its delay from the previous step's send". Measuring from the *scheduled*
    time instead would let worker lag compress every later gap.
    """

    peer_field = "sequence"

    sequence = models.ForeignKey(Sequence, on_delete=models.CASCADE, related_name="enrollments")
    contact = models.ForeignKey("contacts.Contact", on_delete=models.CASCADE, related_name="sequence_enrollments")
    current_step = models.PositiveIntegerField(default=1)
    next_run_at = models.DateTimeField(null=True, blank=True)
    last_sent_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=EnrollmentStatus.choices, default=EnrollmentStatus.ACTIVE)

    class Meta:
        db_table = "campaigns_sequence_enrollment"
        ordering = ["-created_at"]
        constraints = [
            # Partial: one live enrollment per pair, any number of historical
            # ones. Re-enrollment therefore has to retire the previous row
            # before it can create the next, which is exactly SPEC §12's rule
            # enforced by the database rather than by remembering.
            models.UniqueConstraint(
                fields=["sequence", "contact"],
                condition=models.Q(status=EnrollmentStatus.ACTIVE),
                name="enrollment_one_active_per_contact",
            ),
        ]
        indexes = [
            # SPEC §5 names this index and SPEC §12 names the query it serves:
            # `status='active' AND next_run_at <= now()`, claimed SKIP LOCKED.
            models.Index(fields=["status", "next_run_at"], name="enrollment_due_idx"),
            models.Index(fields=["workspace", "sequence"], name="enrollment_ws_sequence_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.contact_id} in {self.sequence_id} ({self.status})"


class RuleTriggerFire(ContactScopedModel):
    """When a rule trigger last fired for a contact — the loop-safety cooldown.

    A durable "may I do this once in the last minute" question, answered by the
    database rather than by a read followed by a write, exactly as
    ``apps/flows/triggers/guards.py::claim_default_reply`` answers SPEC §10's
    24-hour default-reply guard. The receiver runs inside whichever transaction
    emitted the event, so two concurrent writes to one contact really do race.

    It lives in this app rather than beside that guard so this issue's only
    migration is in its own app — same-layer workstreams sharing an app is what
    makes parallel migrations unsafe (ROADMAP).
    """

    peer_field = "trigger"

    trigger = models.ForeignKey("flows.Trigger", on_delete=models.CASCADE, related_name="rule_fires")
    contact = models.ForeignKey("contacts.Contact", on_delete=models.CASCADE, related_name="rule_trigger_fires")
    last_fired_at = models.DateTimeField()

    class Meta:
        db_table = "campaigns_rule_trigger_fire"
        constraints = [
            models.UniqueConstraint(fields=["trigger", "contact"], name="rulefire_unique_trigger_contact"),
        ]

    def __str__(self) -> str:
        return f"{self.trigger_id} fired for {self.contact_id} at {self.last_fired_at:%Y-%m-%d %H:%M:%S}"
