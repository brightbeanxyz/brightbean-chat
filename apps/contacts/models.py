"""The CRM core: contacts, tags, custom fields and segments (SPEC §5).

Every model here is tenant data, so every one inherits
:class:`apps.common.scoping.WorkspaceScopedModel` — including the two join
tables. ``contact_tag`` and ``custom_field_value`` carry a ``workspace`` column
that SPEC §5 does not list, and it is not redundant bookkeeping: the condition
engine filters them inside correlated ``Exists()`` subqueries, and a subquery is
*compiled* rather than executed, so ``WorkspaceScopedQuerySet``'s guard never
fires on one (:mod:`apps.contacts.conditions` explains at length). Carrying the
column means those subqueries can be scoped for real instead of resting on a
join back to ``contact``.

Deliberately **not** here:

* ``contact_channel_identity`` — SPEC §5 files it under contacts, but it hangs
  off a channel connection, so it lands with the messaging spine (issue #8).
* Hard delete and export. ``status`` is a soft-delete flag; GDPR erasure is
  issue #29, which needs identities and message bodies to mean anything.

Four shape decisions, each argued in the PR:

1. ``Tag.name`` and ``CustomField.name`` are unique **case-insensitively**,
   stricter than SPEC's ``unique (workspace_id, name)``. A CRM where "VIP" and
   "vip" are two tags is a data-quality bug visible in the first tag picker, and
   a service-layer check for it races with itself.
2. The five typed columns on ``CustomFieldValue`` are all nullable, ``value_text``
   included. Django discourages nullable text because it creates two empty
   states — but that is the point here: NULL means "this row is not a text row",
   a different fact from "the text is empty", and it is what makes the
   exactly-one-populated check expressible at all.
3. That check **is** a ``CheckConstraint`` — the project's first. Prose would not
   do: if a writer ever put a number into ``value_text``, the condition engine
   would read ``value_number``, find NULL, and silently return the wrong set of
   contacts. A broadcast then goes to the wrong people with nothing raising
   anywhere. The database is the only place that can catch it.
4. ``Contact`` has no ``Meta.ordering``. Ordering on a model the condition
   engine filters set-wise would attach an ``ORDER BY`` to every query it builds,
   counts included, for no benefit. The list views order explicitly. The two
   join tables skip it for a sharper reason: an ``ordering`` that spans a
   relation (``["tag__name"]``) would drag a JOIN into every ``Exists()``
   subquery.
"""

from typing import TYPE_CHECKING, Any, ClassVar

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxLengthValidator
from django.db import models
from django.db.models.functions import Lower
from django.db.models.signals import m2m_changed
from django.dispatch import receiver

from apps.common.scoping import WorkspaceScopedModel
from apps.contacts.errors import WorkspaceMismatchError


class ContactStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    DELETED = "deleted", "Deleted"


class CustomFieldType(models.TextChoices):
    """The value types a custom field can hold (SPEC §5, SPEC §11.4)."""

    TEXT = "text", "Text"
    NUMBER = "number", "Number"
    DATE = "date", "Date"
    DATETIME = "datetime", "Date and time"
    BOOLEAN = "boolean", "True or false"


#: Which column on ``CustomFieldValue`` holds a value of each field type.
#:
#: The condition engine builds ORM lookups by joining a name from this table to
#: an operator from its own allowlist, so a user-supplied field key can never
#: become part of a query kwarg. The check constraint below is generated from
#: the same table, so the two cannot drift.
VALUE_COLUMNS: dict[str, str] = {
    CustomFieldType.TEXT.value: "value_text",
    CustomFieldType.NUMBER.value: "value_number",
    CustomFieldType.DATE.value: "value_date",
    CustomFieldType.DATETIME.value: "value_datetime",
    CustomFieldType.BOOLEAN.value: "value_bool",
}

ALL_VALUE_COLUMNS: tuple[str, ...] = tuple(VALUE_COLUMNS[t] for t in CustomFieldType.values)

#: Cap on a stored text value (SECURITY-BASELINE §7). The condition engine caps
#: a *filter* value lower, at 500: a value can be longer than anything anyone
#: would sensibly compare it against in full.
MAX_TEXT_VALUE_CHARS = 2000


def exactly_one_value_populated() -> models.Q:
    """Exactly one of the five typed columns is non-NULL.

    Written as a plain ``Q`` rather than ``num_nonnulls(...)`` in raw SQL:
    SECURITY-BASELINE §7 bans string-built SQL, and a ``RawSQL`` check would be
    exactly that. Generated from :data:`ALL_VALUE_COLUMNS` so adding a field type
    cannot leave the constraint behind.
    """
    clauses = models.Q()
    for populated in ALL_VALUE_COLUMNS:
        clause = models.Q(**{f"{populated}__isnull": False})
        for other in ALL_VALUE_COLUMNS:
            if other != populated:
                clause &= models.Q(**{f"{other}__isnull": True})
        clauses |= clause
    return clauses


class Contact(WorkspaceScopedModel):
    """One person in a workspace's CRM."""

    first_name = models.CharField(max_length=150, blank=True, default="")
    last_name = models.CharField(max_length=150, blank=True, default="")
    locale = models.CharField(max_length=16, blank=True, default="")
    timezone = models.CharField(max_length=63, blank=True, default="")
    # Not unique and not checked for deliverability: a contact's email is
    # whatever the platform or the operator supplied. Identity uniqueness is the
    # messaging spine's (issue #8), deliverability the email adapter's (#21).
    email = models.EmailField(max_length=254, blank=True, default="")
    phone = models.CharField(max_length=32, blank=True, default="")
    status = models.CharField(max_length=16, choices=ContactStatus.choices, default=ContactStatus.ACTIVE)
    last_interaction_at = models.DateTimeField(null=True, blank=True)

    # Read-only, and enforced rather than asked for — see the m2m_changed
    # receiver at the bottom of this module. It exists for reading (
    # ``prefetch_related("tags")``, ``Count("contacts")``); every write goes
    # through :mod:`apps.contacts.services` so the contract-7 events fire.
    tags = models.ManyToManyField("contacts.Tag", through="contacts.ContactTag", related_name="contacts", blank=True)

    class Meta:
        db_table = "contacts_contact"
        indexes = [
            # SPEC §5 names this one: the CRM list and every recency-ordered
            # sweep read it.
            models.Index(fields=["workspace", "last_interaction_at"], name="contact_ws_last_seen_idx"),
            # Every condition query opens with `workspace_id = %s AND status =
            # 'active'` (see conditions.queryset), so this one is on the hot path
            # of the whole engine.
            models.Index(fields=["workspace", "status"], name="contact_ws_status_idx"),
            # The two system_field sources most likely to carry an equality
            # filter at 10k+ rows.
            models.Index(fields=["workspace", "email"], name="contact_ws_email_idx"),
            models.Index(fields=["workspace", "phone"], name="contact_ws_phone_idx"),
            # Newest-first, which is how the public API lists contacts (#25) and
            # how an integrator pages a whole workspace on an incremental sync.
            # Without it that ordering is a sort of every matching row, repeated
            # for each page; none of the indexes above can serve it, because
            # they are all keyed on a different second column.
            models.Index(fields=["workspace", "-created_at"], name="contact_ws_created_idx"),
        ]

    def __str__(self) -> str:
        return self.display_name

    @property
    def display_name(self) -> str:
        """A label that is never empty, so a list never renders a blank row."""
        name = f"{self.first_name} {self.last_name}".strip()
        return name or self.email or self.phone or f"Contact {str(self.pk)[:8]}"


class Tag(WorkspaceScopedModel):
    """A label an operator or a flow can attach to a contact."""

    name = models.CharField(max_length=100)

    class Meta:
        db_table = "contacts_tag"
        ordering = ["name"]
        constraints = [
            # Lower(name) rather than SPEC's plain (workspace, name): see the
            # module docstring. full_clean() validates expression constraints,
            # so forms and the admin report the clash rather than a 500.
            models.UniqueConstraint(Lower("name"), "workspace", name="tag_unique_name_per_workspace"),
        ]

    def __str__(self) -> str:
        return self.name


class ContactScopedModel(WorkspaceScopedModel):
    """Abstract base for rows that hang off a ``Contact`` and inherit its tenancy.

    ``workspace`` on these tables is a denormalisation the enforcing manager
    requires — ``for_workspace()`` filters ``workspace_id`` — and a
    denormalisation is a chance for three columns to disagree.
    ``ContactTag.workspace``, ``.contact.workspace`` and ``.tag.workspace`` are
    three separate answers to "whose row is this?", and a row where they differ
    is a tenancy bug no test stumbles over by accident.

    So ``workspace`` is **derived, never set by a caller** — the same discipline
    ``CredentialMixin.save()`` uses for ``is_configured``, ``update_fields``
    branch and all — and the peer foreign key is checked against the same
    contact.

    The stronger guarantee, a composite foreign key
    ``(workspace_id, contact_id) REFERENCES contacts_contact (workspace_id, id)``,
    is genuinely better and genuinely unavailable: Django 5.2 has no composite
    foreign key to non-primary-key columns, so it would mean hand-written
    ``RunSQL`` plus two extra unique indexes. Not worth it while the write path
    is one function.

    Not covered: ``bulk_create``, which bypasses ``save()``. Nothing in this app
    bulk-creates; issue #13's CSV importer must set ``workspace`` itself if it
    does.

    Declares no managers, so ``all_objects`` keeps the lowest creation counter
    and ``apps.common.checks`` (``common.E004``) stays satisfied.
    """

    #: Name of the foreign key whose workspace must agree with the contact's.
    #: The peer may be nullable: issue #8's ``ContactChannelIdentity`` carries a
    #: connection-less "pending" state (ROADMAP contract 1 — an address captured
    #: before any connection of that platform exists, upgraded lazily at first
    #: send). A null peer has no workspace to disagree with, so the check below
    #: skips rather than raising; the derivation from ``contact`` still runs, and
    #: it is the derivation that the tenancy of these rows actually rests on.
    peer_field: ClassVar[str] = ""

    if TYPE_CHECKING:
        # Declared by every concrete subclass. Annotated rather than assigned
        # so Django's model machinery never sees a non-field attribute here.
        contact: Any

    class Meta:
        abstract = True

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.workspace_id = self.contact.workspace_id
        peer = getattr(self, self.peer_field)
        if peer is not None and peer.workspace_id != self.contact.workspace_id:
            raise WorkspaceMismatchError(
                f"That {peer._meta.verbose_name} belongs to a different workspace than the contact."
            )
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            # Only widen a non-empty set. Django reads a falsy ``update_fields``
            # as "save nothing" and returns before touching the database or
            # sending signals (``django/db/models/base.py``: "If update_fields
            # is empty, skip the save"), so adding ``workspace`` to an empty one
            # would turn a documented no-op into a real UPDATE.
            widened = set(update_fields)
            kwargs["update_fields"] = widened | {"workspace"} if widened else widened
        super().save(*args, **kwargs)


class ContactTag(ContactScopedModel):
    """The contact ↔ tag join. Written only through :mod:`apps.contacts.services`."""

    peer_field = "tag"

    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name="contact_tags")
    tag = models.ForeignKey(Tag, on_delete=models.CASCADE, related_name="contact_tags")

    class Meta:
        db_table = "contacts_contact_tag"
        constraints = [
            # SPEC §5's unique-together, and also the index the condition
            # engine's correlated probe rides: `contact_id = c.id AND tag_id =
            # %s` is equality on both leading columns. Do not reorder it.
            models.UniqueConstraint(fields=["contact", "tag"], name="contacttag_unique_contact_tag"),
        ]
        indexes = [
            # The other direction — "everyone with tag X". Covering, so Postgres
            # can run the EXISTS as an index-only semi-join. workspace leads
            # because the compiled subquery always carries `workspace_id = %s`.
            models.Index(fields=["workspace", "tag", "contact"], name="contacttag_ws_tag_contact_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.contact} · {self.tag}"


class CustomField(WorkspaceScopedModel):
    """A workspace-defined attribute that contacts can carry."""

    name = models.CharField(max_length=100)
    type = models.CharField(max_length=16, choices=CustomFieldType.choices)

    class Meta:
        db_table = "contacts_custom_field"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(Lower("name"), "workspace", name="customfield_unique_name_per_workspace"),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.get_type_display()})"

    @property
    def value_column(self) -> str:
        """The ``CustomFieldValue`` column this field's values live in."""
        return VALUE_COLUMNS[self.type]


class CustomFieldValue(ContactScopedModel):
    """One contact's value for one custom field.

    Exactly one of the five typed columns is populated, chosen by the field's
    ``type``, and the database enforces the "exactly one" half. The database
    cannot enforce *which* one, because the deciding type lives on another table
    — that half is held by :func:`apps.contacts.services.set_field_value` (the
    only write path, which clears the other four on every write) and by
    ``clean()``. Clearing a value deletes the row rather than nulling it, which
    is what keeps "exactly one" true rather than "at most one".
    """

    peer_field = "field"

    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name="field_values")
    field = models.ForeignKey(CustomField, on_delete=models.CASCADE, related_name="values")

    value_text = models.TextField(null=True, blank=True, validators=[MaxLengthValidator(MAX_TEXT_VALUE_CHARS)])
    value_number = models.DecimalField(max_digits=20, decimal_places=6, null=True, blank=True)
    value_date = models.DateField(null=True, blank=True)
    value_datetime = models.DateTimeField(null=True, blank=True)
    value_bool = models.BooleanField(null=True, blank=True)

    class Meta:
        db_table = "contacts_custom_field_value"
        constraints = [
            models.UniqueConstraint(fields=["contact", "field"], name="customfieldvalue_unique_contact_field"),
            models.CheckConstraint(
                condition=exactly_one_value_populated(),
                name="customfieldvalue_exactly_one_value",
            ),
        ]
        indexes = [
            models.Index(fields=["workspace", "field", "contact"], name="cfv_ws_field_contact_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.contact} · {self.field}"

    @property
    def value(self) -> Any:
        """The populated column's value, whichever one it is."""
        return getattr(self, self.field.value_column)

    def clean(self) -> None:
        populated = [column for column in ALL_VALUE_COLUMNS if getattr(self, column) is not None]
        if len(populated) != 1:
            raise ValidationError(
                f"A custom field value populates exactly one column; this row populates {len(populated)}."
            )
        expected = self.field.value_column
        if populated[0] != expected:
            raise ValidationError(f"A {self.field.get_type_display().lower()} field stores its value in {expected}.")


class Segment(WorkspaceScopedModel):
    """A saved condition filter over a workspace's contacts (SPEC §5, §11.4).

    ``filter_json`` uses the same schema as the flow Condition node, and
    ``clean()`` validates it through :mod:`apps.contacts.conditions`, so neither
    a form nor the Django admin can store a filter the engine would later refuse
    to compile.
    """

    name = models.CharField(max_length=100)
    filter_json = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "contacts_segment"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(Lower("name"), "workspace", name="segment_unique_name_per_workspace"),
        ]

    def __str__(self) -> str:
        return self.name

    def clean(self) -> None:
        # Imported here rather than at module scope: conditions.py reads these
        # models, so a top-level import would be a cycle.
        from apps.contacts.conditions import ConditionValidationError, validate

        if self.workspace_id is None:
            return
        try:
            validate(self.workspace_id, self.filter_json, exclude_segment_id=self.pk)
        except ConditionValidationError as exc:
            raise ValidationError({"filter_json": str(exc)}) from exc


class ImportStatus(models.TextChoices):
    """Where a CSV import has got to (SPEC §2: contacts includes import/export).

    ``validating`` and ``importing`` are the two states a worker moves through,
    and they are distinct because only the second one writes. An operator who
    reloads the page mid-run has to be able to tell "we are checking your file"
    from "we are creating your contacts", because only one of those is worth
    interrupting.
    """

    UPLOADED = "uploaded", "Uploaded"
    VALIDATING = "validating", "Checking"
    VALIDATED = "validated", "Checked"
    IMPORTING = "importing", "Importing"
    DONE = "done", "Finished"
    FAILED = "failed", "Failed"


#: Statuses no worker will move on from. Read by the housekeeping prune.
FINISHED_IMPORT_STATUSES: frozenset[str] = frozenset({ImportStatus.DONE.value, ImportStatus.FAILED.value})


class ImportDedupe(models.TextChoices):
    """What to do with a row whose email or phone already names a contact.

    The choice is the operator's rather than a house rule, because both answers
    are right for different files: a re-export from another CRM should update,
    and a list of new leads that happens to share an address with an existing
    contact may genuinely be a second person at a shared inbox.
    """

    UPDATE = "update", "Update the contact that is already there"
    CREATE = "create", "Create a second contact anyway"
    SKIP = "skip", "Skip the row"


def import_upload_to(instance: "ContactImport", filename: str) -> str:
    """``contact-imports/<workspace>/<import id>.csv`` — ``filename`` is ignored.

    The parameter exists because Django's ``upload_to`` protocol passes it, and
    ignoring it is the point: every component of the stored path is a value this
    server chose, so an uploaded name of ``../../etc/passwd`` decides nothing.
    ``original_filename`` keeps the human-readable half, escaped at render.

    Copied in shape from ``apps.media_library.storage.asset_upload_to`` rather
    than imported from it: that module's path is ``media/…`` and its extension
    comes from the media MIME table, neither of which applies here.
    """
    return f"contact-imports/{instance.workspace_id}/{instance.pk}.csv"


class ContactImport(WorkspaceScopedModel):
    """One CSV import run: the file, the mapping, the progress and the errors.

    A row rather than a session key because the work is a background job (SPEC
    §15) that outlives the request that started it, and because the operator has
    to be able to come back to the report. The four counters and ``next_offset``
    are the resume point: :mod:`apps.contacts.imports` processes a bounded slice
    of rows per queued action and re-schedules itself, so a 50 000-row file never
    sits inside one web request *or* one long database transaction.

    ``errors`` is a **capped** list. A file where every row is malformed would
    otherwise put fifty thousand error objects in a jsonb column — the row would
    be larger than the upload that caused it. Past
    :data:`MAX_REPORTED_ROW_ERRORS` the entries stop accumulating and
    ``error_count`` keeps counting, so the report says how many it is not
    showing rather than quietly appearing complete.

    Not covered here: identities. An imported contact is **not reachable on any
    channel** until they message in, and this app never fabricates a
    ``ContactChannelIdentity`` to pretend otherwise — see
    :mod:`apps.contacts.imports` and the consent copy on the upload step.
    """

    #: How many row errors are stored in full. The rest are counted only.
    MAX_REPORTED_ROW_ERRORS: ClassVar[int] = 1000

    file = models.FileField(upload_to=import_upload_to, max_length=512)
    #: As uploaded. Attacker-controlled text (SECURITY-BASELINE §2): displayed
    #: escaped, never used to build a path — see :func:`import_upload_to`.
    original_filename = models.CharField(max_length=255, blank=True, default="")

    status = models.CharField(max_length=16, choices=ImportStatus.choices, default=ImportStatus.UPLOADED)

    #: ``{csv column name: target}``, where target is ``system:<name>``,
    #: ``field:<uuid>`` or ``tags``. Absent means "ignore this column".
    #: Validated by :mod:`apps.contacts.imports` on every read, not only on the
    #: write that stored it: a mapping naming a custom field somebody has since
    #: deleted is a document that was valid when it was saved.
    mapping = models.JSONField(default=dict, blank=True)
    dedupe = models.CharField(max_length=16, choices=ImportDedupe.choices, default=ImportDedupe.UPDATE)

    #: The operator ticked the box saying imported contacts are not
    #: channel-reachable. Stored rather than merely required, because it is the
    #: record that the person who imported the list was told.
    consent_ack = models.BooleanField(default=False)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="contact_imports",
    )

    total_rows = models.PositiveIntegerField(default=0)
    processed_rows = models.PositiveIntegerField(default=0)
    created_count = models.PositiveIntegerField(default=0)
    updated_count = models.PositiveIntegerField(default=0)
    skipped_count = models.PositiveIntegerField(default=0)
    error_count = models.PositiveIntegerField(default=0)

    #: The next data row (0-based, header excluded) a batch should start at.
    next_offset = models.PositiveIntegerField(default=0)

    #: ``[{"row": int, "column": str, "message": str}]``, capped — see the class
    #: docstring.
    errors = models.JSONField(default=list, blank=True)

    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "contacts_contact_import"
        ordering = ["-created_at"]
        indexes = [
            # The housekeeping prune: finished runs older than the retention
            # window, across every workspace.
            models.Index(fields=["status", "finished_at"], name="contactimport_status_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.original_filename or 'import'} ({self.get_status_display()})"

    @property
    def is_running(self) -> bool:
        """A worker is checking or importing this file. Drives the progress poll."""
        return self.status in {ImportStatus.VALIDATING, ImportStatus.IMPORTING}

    @property
    def is_writing(self) -> bool:
        """Contacts are being created or updated right now.

        Narrower than :attr:`is_running` on purpose, and it is what gates
        re-mapping. A dry run in flight writes nothing, so an operator who spots
        a mistake in their column mapping should be able to fix it and check
        again — the new pass resets ``next_offset``, which strands the old pass's
        next batch on the offset guard. An import in flight is a different
        matter: its mapping is already half-applied to real rows.
        """
        return self.status == ImportStatus.IMPORTING

    @property
    def errors_truncated(self) -> bool:
        """More rows failed than the report stores in full."""
        return self.error_count > len(self.errors)

    @property
    def hidden_error_count(self) -> int:
        """How many row errors were counted but not recorded."""
        return max(0, self.error_count - len(self.errors))

    @property
    def percent_complete(self) -> int:
        """Progress for the bar. 0 while the row count is still unknown."""
        if not self.total_rows:
            return 0
        return min(100, round(self.processed_rows * 100 / self.total_rows))


class ErasureStatus(models.TextChoices):
    """The lifecycle of one erasure request."""

    PENDING = "pending", "Pending"
    RUNNING = "running", "Running"
    DONE = "done", "Done"
    FAILED = "failed", "Failed"


class ErasureSource(models.TextChoices):
    """Which surface asked for it. Part of the audit answer, not decoration."""

    UI = "ui", "Contact page"
    BULK = "bulk", "Bulk action"
    API = "api", "Public API"


class ContactErasure(WorkspaceScopedModel):
    """Who erased which contact, when, and what went — SPEC §19, issue #29.

    The first audit table in the product, and it exists because erasure is the
    one act with no undo. ``delete_contact`` sets a flag and every row survives;
    this removes a person, their identities, their consent records and their
    message history outright. "It was done" has to remain answerable after the
    only evidence has been deleted, so the receipt is a row of its own rather
    than a log line.

    --------------------------------------------------------------------------
    What it deliberately does not hold
    --------------------------------------------------------------------------

    **No name, no email, no phone.** A record that survives an erasure by
    keeping the erased person's identifiers has not erased them; it has moved
    them. :attr:`contact_id` is the whole reference — a UUID this deployment
    minted, which after the delete resolves to nothing and identifies nobody. It
    is enough to answer "was this request honoured", which is the question an
    audit is for, and not enough to reconstruct who it was about.

    ``requested_by_label`` is the exception that proves it: that is the
    *operator's* address, not the contact's. Accountability runs the other way.

    --------------------------------------------------------------------------
    Two jobs, one row
    --------------------------------------------------------------------------

    It is also the queue-backed run's state, the way :class:`ContactImport` is
    for a CSV import — a large contact's erasure outlives the request that asked
    for it, so ``status`` and ``error`` are here rather than in a second table
    that would have to be kept in step with this one.

    No foreign key to ``Contact``: the row it names is gone by the time this one
    matters, and a cascade would delete exactly the record that has to survive —
    the reasoning ``apps/channels/models.py``'s ``EmailSuppression`` gives for
    the same choice.
    """

    #: The erased contact. A plain UUID, for the reason in the class docstring.
    contact_id = models.UUIDField(db_index=True)

    #: The operator, while they still have an account. ``SET_NULL`` because the
    #: audit outlives the membership — the same reading ``ApiKey.created_by``
    #: takes, and why the label below is stored beside it rather than derived.
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="contact_erasures",
        help_text="Audit only. Null once the account goes; see requested_by_label.",
    )
    #: Who they were at the time. Denormalised on purpose: a foreign key alone
    #: answers "nobody" after the account is removed, which is the moment an
    #: audit trail is most often read.
    requested_by_label = models.CharField(max_length=254, blank=True, default="")

    source = models.CharField(max_length=8, choices=ErasureSource.choices)

    #: Which API key, when ``source`` is ``api``. A plain UUID rather than a
    #: foreign key so ``apps.contacts`` does not grow an import of ``apps.api``.
    api_key_id = models.UUIDField(null=True, blank=True)

    status = models.CharField(max_length=8, choices=ErasureStatus.choices, default=ErasureStatus.PENDING)
    completed_at = models.DateTimeField(null=True, blank=True)

    #: ``{"messaging.Message": 412, ...}`` — the anonymised receipt. Row counts
    #: carry no personal data and are what makes "it removed what it claimed"
    #: checkable a year later.
    counts = models.JSONField(default=dict, blank=True)

    error = models.TextField(blank=True, default="")

    class Meta:
        db_table = "contacts_contact_erasure"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["workspace", "-created_at"], name="erasure_ws_created_idx"),
        ]

    def __str__(self) -> str:
        return f"erasure of {self.contact_id} ({self.get_status_display()})"


@receiver(m2m_changed, sender=Contact.tags.through)
def _refuse_direct_tag_mutation(sender: Any, action: str, **kwargs: Any) -> None:
    """Make ``Contact.tags``'s read-only contract real instead of advisory.

    ``contact.tags.add(tag)`` was already loud by accident — ``bulk_create``
    skips ``ContactScopedModel.save()``, so the NOT NULL ``workspace`` column
    rejects it. ``.remove()`` and ``.clear()`` only DELETE, so they *succeeded*,
    dropping link rows with no ``contact.tag_removed`` emitted. Issue #22's rule
    triggers and #25's outbound webhooks would simply never learn the contact
    lost the tag, and nothing would raise — a silent failure exactly where the
    docstring implied safety.

    A ``RuntimeError`` rather than a :class:`~apps.contacts.errors.ContactsError`
    on purpose: this is a programming mistake, not user input, and views catch
    ``ContactsError`` to render a toast.
    """
    if action in {"pre_add", "pre_remove", "pre_clear"}:
        raise RuntimeError(
            "Contact.tags is read-only. Use apps.contacts.services.add_tag / remove_tag, "
            "which emit the contact.tag_added / contact.tag_removed events that issue #22's "
            "rule triggers and #25's webhooks subscribe to."
        )
