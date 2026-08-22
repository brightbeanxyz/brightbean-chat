"""The public API later layers call, and the only place contact events are sent.

Every mutation to a contact, tag, custom field or segment goes through a
function here rather than through the ORM directly. That is not tidiness: the
internal event catalog (:mod:`apps.contacts.events`, ROADMAP contract 7) is what
issue #22's rule triggers and #25's outbound webhooks subscribe to, and a write
that bypasses this module is a change the rest of the product never learns
about. ``contact.tags.add(tag)`` is the tempting version of that mistake, which
is why :class:`~apps.contacts.models.Contact.tags` is documented as read-only.

Refusals are :class:`apps.contacts.errors.ContactsError` subclasses — a
``ValueError`` with a message written for an end user, which views surface with
``messages.error(request, str(exc))`` exactly as ``apps/members/views.py`` does.

Idempotency is part of the contract, not an optimisation. Re-adding a tag a
contact already has returns ``False`` and sends **no** event: the event means
"this contact newly has this tag", and re-sending it would turn a re-run flow
into a duplicate webhook delivery — and, once issue #22 lands rule triggers,
into a loop, because a rule triggered by ``tag_added`` whose action re-adds the
tag would trigger itself for ever.

Not here, deliberately:

* Deduplication on create. Identity-based dedup is issue #8's — a channel
  identity is the natural key — and import dedup is :mod:`apps.contacts.imports`',
  which matches on the email or phone the operator mapped. A third rule invented
  here is one both of them would have to unlearn.
* Hard deletion. :func:`delete_contact` sets ``status``; erasing the row, its
  messages and its identities is issue #29's GDPR work.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.contacts.errors import ContactsError, FieldTypeError, WorkspaceMismatchError
from apps.contacts.events import (
    EVENT_CONTACT_CREATED,
    EVENT_CONTACT_FIELD_CHANGED,
    EVENT_CONTACT_TAG_ADDED,
    EVENT_CONTACT_TAG_REMOVED,
    emit,
)
from apps.contacts.models import (
    MAX_TEXT_VALUE_CHARS,
    VALUE_COLUMNS,
    Contact,
    ContactStatus,
    ContactTag,
    CustomField,
    CustomFieldType,
    CustomFieldValue,
    Segment,
    Tag,
)
from apps.flows.compat import installed_model

#: How a contact came to exist. Mirrors SPEC §11.8's ``opt_in_source`` vocabulary
#: so the two do not drift into two different words for the same thing.
CONTACT_SOURCES: frozenset[str] = frozenset({"manual", "api", "import", "flow", "inbound"})

#: The contact columns a human may edit — the allowlist ``update_contact``
#: enforces and the detail page renders. Public because the view and the CSV
#: importer both build their inputs from it, and a second list of six strings is
#: a second thing to forget when a seventh column arrives.
EDITABLE_FIELDS: tuple[str, ...] = ("first_name", "last_name", "email", "phone", "locale", "timezone")

#: The old private spelling, kept because ``merge_contacts`` reads it.
_SCALAR_FIELDS: tuple[str, ...] = EDITABLE_FIELDS

#: Column widths. Both write paths read this table rather than repeating the
#: literals, so a contact typed into the detail page and one created by an import
#: are normalised identically — and a column widened in ``models.py`` has one
#: place here to follow it.
_SCALAR_LIMITS: dict[str, int] = {
    "first_name": 150,
    "last_name": 150,
    "email": 254,
    "phone": 32,
    "locale": 16,
    "timezone": 63,
}


def _clean_text(value: Any, *, limit: int) -> str:
    """Collapse whitespace and **truncate** to the column width.

    Truncating rather than raising is right for the fields this is used on —
    a contact's name, email and phone as a platform hands them over. Rejecting
    the whole contact because a display name is 300 characters would drop an
    inbound message; keeping the first 150 loses nothing anyone will miss.

    Names a human types are the opposite case: see :func:`_clean_name`.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ContactsError("Expected text.")
    # Postgres text cannot hold a NUL, and `split()` does not treat one as
    # whitespace, so without this it survives into `objects.create()` and psycopg
    # raises at execute time — a 500 for a value an inbound platform or a CSV
    # import supplied. The custom-field and condition paths already refuse it.
    if "\x00" in value:
        raise ContactsError("Text cannot contain a null byte.")
    return " ".join(value.split())[:limit]


def _clean_name(value: Any, *, limit: int, noun: str) -> str:
    """Collapse whitespace and **refuse** anything over the column width.

    Silently truncating a name a human typed is worse than refusing it twice
    over: they see a name they did not type, and two names that differ only
    after the limit collapse into one, so the second attempt is rejected as a
    duplicate of a name that looks nothing like it on screen. This is also what
    ``coerce_value`` already does for a custom-field text value, so the two
    paths now agree that over-length input is an error rather than a suggestion.
    """
    if not isinstance(value, str) and value is not None:
        raise ContactsError(f"A {noun} name must be text.")
    if value and "\x00" in value:
        raise ContactsError(f"A {noun} name cannot contain a null byte.")
    cleaned = " ".join((value or "").split())
    if not cleaned:
        raise ContactsError(f"A {noun} needs a name.")
    if len(cleaned) > limit:
        raise ContactsError(f"A {noun} name is at most {limit} characters.")
    return cleaned


def _assert_name_is_free(model: Any, workspace_id: Any, name: str, *, noun: str, excluding: Any = None) -> None:
    """Refuse a name another row in the workspace already holds.

    Matched case-insensitively, because the unique constraints are on
    ``Lower(name)`` — checking with ``=`` here would let "vip" through and then
    let the database raise on it.
    """
    rows = model.objects.for_workspace(workspace_id).filter(name__iexact=name)
    if excluding is not None:
        rows = rows.exclude(pk=excluding)
    if rows.exists():
        raise ContactsError(f"A {noun} with that name already exists.")


@contextmanager
def _unique_name(noun: str) -> Iterator[None]:
    """Turn the unique-index violation the check above races with into a refusal.

    ``_assert_name_is_free`` is a check-then-write, so two concurrent requests
    can both pass it. Without this the loser gets an ``IntegrityError`` — a 500
    for input the single-threaded path answers with a readable message — and
    poisons any enclosing atomic block. The savepoint keeps that block usable.
    """
    try:
        with transaction.atomic():
            yield
    except IntegrityError as exc:
        raise ContactsError(f"A {noun} with that name already exists.") from exc


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------


def create_contact(
    workspace: Any,
    *,
    first_name: str = "",
    last_name: str = "",
    email: str = "",
    phone: str = "",
    locale: str = "",
    contact_timezone: str = "",
    last_interaction_at: datetime | None = None,
    source: str = "manual",
) -> Contact:
    """Create a contact and send ``contact.created``.

    ``email`` is lowercased so the ``(workspace, email)`` index stays usable as
    an equality probe rather than needing a functional one.
    """
    if source not in CONTACT_SOURCES:
        raise ContactsError(f"Unknown contact source {source!r}.")
    with transaction.atomic():
        contact = Contact.objects.create(
            workspace=workspace,
            first_name=_clean_text(first_name, limit=_SCALAR_LIMITS["first_name"]),
            last_name=_clean_text(last_name, limit=_SCALAR_LIMITS["last_name"]),
            email=_clean_text(email, limit=_SCALAR_LIMITS["email"]).lower(),
            phone=_clean_text(phone, limit=_SCALAR_LIMITS["phone"]),
            locale=_clean_text(locale, limit=_SCALAR_LIMITS["locale"]),
            timezone=_clean_text(contact_timezone, limit=_SCALAR_LIMITS["timezone"]),
            last_interaction_at=last_interaction_at,
        )
        emit(
            EVENT_CONTACT_CREATED,
            workspace_id=contact.workspace_id,
            contact_id=contact.pk,
            source=source,
        )
    return contact


def merge_contacts(*, primary: Contact, duplicate: Contact) -> Contact:
    """Fold ``duplicate`` into ``primary`` and soft-delete it.

    The survivor is authoritative throughout: tags are unioned, but field values
    and scalar columns only fill gaps, because a merge must never overwrite
    something a human just typed. ``last_interaction_at`` takes the later of the
    two.

    The duplicate becomes ``status=deleted`` rather than being removed. SPEC §5
    put ``deleted`` in the enum for exactly this, and issue #29 owns hard delete
    and GDPR export. Its own tag and field rows stay with it: it is a tombstone,
    and every read surface starts from active contacts.

    Messaging rows are the exception, and they are re-pointed rather than left
    behind (issue #8). A tag on a tombstone is inert; an *identity* on one keeps
    receiving webhooks while its conversation has vanished from the inbox. The
    logic lives in ``apps.messaging.merge`` so this app keeps knowing nothing
    about messaging, and is reached through ``installed_model`` so this app
    still imports in a deployment without it.
    """
    if primary.workspace_id != duplicate.workspace_id:
        raise WorkspaceMismatchError("Both contacts must belong to the same workspace.")
    if primary.pk == duplicate.pk:
        raise ContactsError("A contact cannot be merged into itself.")
    if primary.status == ContactStatus.DELETED:
        # Merging *into* a tombstone would copy the duplicate's tags and values
        # onto a row no active-contact surface renders, then tombstone the
        # duplicate as well — losing both contacts in one call.
        raise ContactsError("The surviving contact has been deleted.")
    if duplicate.status == ContactStatus.DELETED:
        raise ContactsError("That contact has already been deleted.")

    with transaction.atomic():
        held = set(primary.contact_tags.values_list("tag_id", flat=True))
        for link in duplicate.contact_tags.select_related("tag"):
            if link.tag_id not in held:
                _link_tag(primary, link.tag)

        filled = set(primary.field_values.values_list("field_id", flat=True))
        for value in duplicate.field_values.select_related("field"):
            if value.field_id not in filled:
                set_field_value(primary, value.field, value.value)

        changed: list[str] = []
        for name in _SCALAR_FIELDS:
            if not getattr(primary, name) and getattr(duplicate, name):
                setattr(primary, name, getattr(duplicate, name))
                changed.append(name)

        seen = [t for t in (primary.last_interaction_at, duplicate.last_interaction_at) if t is not None]
        if seen and primary.last_interaction_at != max(seen):
            primary.last_interaction_at = max(seen)
            changed.append("last_interaction_at")
        if changed:
            primary.save(update_fields=[*changed, "updated_at"])

        _repoint_messaging(primary, duplicate)

        duplicate.status = ContactStatus.DELETED
        duplicate.save(update_fields=["status", "updated_at"])
    return primary


def _repoint_messaging(primary: Contact, duplicate: Contact) -> None:
    """Hand the merge to ``apps.messaging`` if that app is installed.

    ``installed_model`` is the house pattern for reaching an app that may not be
    there — it answers "not yet" rather than raising, and starts answering for
    real the moment the app is installed, with no edit at this call site.
    """
    if installed_model("messaging", "apps.messaging", "Conversation") is None:
        return
    from apps.messaging.merge import repoint_for_merge

    repoint_for_merge(primary, duplicate)


def delete_contact(contact: Contact) -> bool:
    """Soft-delete a contact. ``True`` when this call is the one that did it.

    ``status = deleted`` rather than a row deletion, which is what SPEC §5 put
    the enum value there for and what ``merge_contacts`` already does to the
    duplicate. Every read surface starts from active contacts, and the condition
    engine refuses to segment on ``status`` at all
    (:mod:`apps.contacts.conditions`), so a tombstone cannot find its way back
    into a send path.

    Contract 7 has **no** ``contact.deleted`` event and this does not invent one:
    the catalog is a wire format that issue #25's outbound webhooks subscribe to
    by name, so adding a key here would be adding it to that format from the
    wrong side of the repository.

    Tag links, field values, identities and messages all stay. They belong to the
    tombstone, they are inert while it is one, and they are what issue #29's
    export has to read to answer a subject-access request. **Live automation does
    not stay** — but stopping it is the caller's move, not this function's, since
    it is the flow engine's business and this app knows nothing about flows. The
    CRM view pairs the two; see ``apps.contacts.activity.stop_automation``.
    """
    if contact.status == ContactStatus.DELETED:
        return False
    contact.status = ContactStatus.DELETED
    contact.save(update_fields=["status", "updated_at"])
    return True


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------


def get_or_create_tag(workspace: Any, name: str) -> tuple[Tag, bool]:
    """Find a tag by name, case-insensitively, or create it."""
    cleaned = _clean_name(name, limit=100, noun="tag")
    existing = Tag.objects.for_workspace(workspace).filter(name__iexact=cleaned).first()
    if existing is not None:
        return existing, False
    try:
        with transaction.atomic():
            return Tag.objects.create(workspace=workspace, name=cleaned), True
    except IntegrityError:
        # Two concurrent creates both missed the probe; the unique index caught
        # the second. The requested state exists, so return it.
        found = Tag.objects.for_workspace(workspace).filter(name__iexact=cleaned).first()
        if found is None:  # pragma: no cover - only reachable if the row vanished
            raise
        return found, False


def rename_tag(tag: Tag, name: str) -> Tag:
    cleaned = _clean_name(name, limit=100, noun="tag")
    _assert_name_is_free(Tag, tag.workspace_id, cleaned, noun="tag", excluding=tag.pk)
    tag.name = cleaned
    with _unique_name("tag"):
        tag.save(update_fields=["name", "updated_at"])
    return tag


def delete_tag(tag: Tag) -> int:
    """Delete a tag and every link to it. Returns how many links went.

    **No ``contact.tag_removed`` per contact**, and that is a decision worth
    knowing about rather than discovering. At ten thousand tagged contacts one
    administrative click would otherwise mean ten thousand rule-trigger
    evaluations and ten thousand webhook deliveries. The event exists to drive
    per-contact automation; a bulk administrative action is not that. Issue #25's
    consumers should treat a tag deletion as a schema change, not as a stream of
    per-contact events.
    """
    # Counts *live* contacts, not link rows, which is why `.delete()`'s own
    # cascade count is not used: it includes links belonging to soft-deleted
    # contacts, and the number goes straight into a message telling an operator
    # how many people they are about to affect.
    live = (
        ContactTag.objects.for_workspace(tag.workspace_id).filter(tag=tag, contact__status=ContactStatus.ACTIVE).count()
    )
    tag.delete()
    return live


def merge_tags(source: Tag, target: Tag) -> int:
    """Fold ``source`` into ``target`` and delete it. Returns how many links moved.

    Two tags that mean the same thing are a data-quality problem the case-
    insensitive unique index cannot catch — "VIP" and "Priority" are distinct
    strings and the same idea — so merging is the repair, and it has to be one
    operation rather than "retag everyone, then delete", which leaves the
    workspace half-migrated if it stops half way.

    Links are **moved, not re-created**: a contact who already carries the target
    simply loses the source link, and one who does not has their existing row
    re-pointed. Deleting and re-adding would work too and would cost one insert
    per contact plus a full round of contract-7 events.

    Which is the second half of the decision: like :func:`delete_tag`, this sends
    **no per-contact event**. At ten thousand tagged contacts one administrative
    click would otherwise mean ten thousand rule-trigger evaluations and ten
    thousand webhook deliveries — and worse than for a deletion, because a merge
    is *both* a removal and an addition, so it would emit two. Issue #25's
    consumers should read a tag merge as a schema change, exactly as they read a
    tag deletion.
    """
    if source.workspace_id != target.workspace_id:
        raise WorkspaceMismatchError("Both tags must belong to the same workspace.")
    if source.pk == target.pk:
        raise ContactsError("A tag cannot be merged into itself.")

    links = ContactTag.objects.for_workspace(source.workspace_id)
    with transaction.atomic():
        # Contacts already carrying the target: their source link is a duplicate
        # the (contact, tag) unique constraint would reject on re-point, so it
        # goes rather than moving.
        already = set(links.filter(tag=target).values_list("contact_id", flat=True))
        links.filter(tag=source, contact_id__in=already).delete()
        # `update` rather than a loop: the rows keep their workspace and contact,
        # so ContactScopedModel.save()'s derivation has nothing left to derive,
        # and a per-row save would be one query per contact for no added
        # invariant.
        moved = links.filter(tag=source).update(tag=target, updated_at=timezone.now())
        source.delete()
    return moved


def update_contact(contact: Contact, **fields: Any) -> list[str]:
    """Write the system fields on a contact. Returns the names that changed.

    The inline editor on issue #13's detail page. Only the six columns in
    :data:`_SCALAR_FIELDS` are writable — an allowlist, not a filtered ``**kwargs``
    pass-through, because the caller is a form POST and ``status`` and
    ``last_interaction_at`` are both columns on this model that a mass assignment
    would otherwise reach.

    Cleaning matches :func:`create_contact` exactly, ``email`` lowercasing
    included, so a contact edited by hand and one created by an import are
    normalised the same way — otherwise the ``(workspace, email)`` index stops
    being usable as an equality probe for exactly the rows a human touched.

    Contract 7 has no ``contact.updated`` event, and this adds none: the catalog
    covers tags and custom fields, which is what automation keys off.
    """
    unknown = sorted(set(fields) - set(_SCALAR_FIELDS))
    if unknown:
        raise ContactsError(f"Not an editable contact field: {', '.join(unknown)}.")

    changed: list[str] = []
    for name, raw in fields.items():
        limit = _SCALAR_LIMITS[name]
        value = _clean_text(raw, limit=limit)
        if name == "email":
            value = value.lower()
        if getattr(contact, name) != value:
            setattr(contact, name, value)
            changed.append(name)
    if changed:
        contact.save(update_fields=[*changed, "updated_at"])
    return changed


def add_tag(contact: Contact, tag: Tag) -> bool:
    """Link ``tag`` to ``contact``. ``True`` when it was newly added.

    Idempotent, and sends ``contact.tag_added`` only on a real change — see the
    module docstring for why re-emitting would be a loop.
    """
    if tag.workspace_id != contact.workspace_id:
        raise WorkspaceMismatchError("That tag belongs to a different workspace than the contact.")
    links = ContactTag.objects.for_workspace(contact.workspace_id)
    if links.filter(contact=contact, tag=tag).exists():
        return False
    return _link_tag(contact, tag)


def _link_tag(contact: Contact, tag: Tag) -> bool:
    """Insert the link and emit, without re-asking whether it is already there.

    Split out for ``merge_contacts``, which has already computed the set of tags
    the survivor holds — going back through ``add_tag`` would spend one probe
    per tag re-establishing what that set says, inside the merge's own
    transaction.
    """
    # Two nested blocks, and the nesting is the point. The inner savepoint wraps
    # the insert *alone*, so the only IntegrityError it can absorb is the
    # (contact, tag) conflict this function knows how to answer. Wrapping the
    # emit() too would mean a subscriber's own integrity failure — once issue #22
    # or #25 connects one — was caught here, rolled back, and reported as "the
    # tag was already there": a receiver failure silently swallowed, which is the
    # opposite of the signal contract.
    #
    # The outer block keeps the link and its subscribers in one unit, so a
    # subscriber that raises still takes the link with it.
    with transaction.atomic():
        try:
            with transaction.atomic():
                # ContactScopedModel.save() derives `workspace` from the contact,
                # so it is deliberately not passed here.
                ContactTag(contact=contact, tag=tag).save()
        except IntegrityError:
            # Two concurrent adds both missed the SELECT. The row exists, which
            # is the requested state; the savepoint keeps the outer block usable.
            return False
        emit(
            EVENT_CONTACT_TAG_ADDED,
            workspace_id=contact.workspace_id,
            contact_id=contact.pk,
            tag_id=tag.pk,
        )
    return True


def remove_tag(contact: Contact, tag: Tag) -> bool:
    """Unlink ``tag`` from ``contact``. ``True`` when a link existed."""
    links = ContactTag.objects.for_workspace(contact.workspace_id).filter(contact=contact, tag=tag)
    with transaction.atomic():
        removed, _ = links.delete()
        if not removed:
            return False
        emit(
            EVENT_CONTACT_TAG_REMOVED,
            workspace_id=contact.workspace_id,
            contact_id=contact.pk,
            tag_id=tag.pk,
        )
    return True


# ---------------------------------------------------------------------------
# Custom fields
# ---------------------------------------------------------------------------


def create_custom_field(workspace: Any, *, name: str, field_type: str) -> CustomField:
    cleaned = _clean_name(name, limit=100, noun="field")
    if field_type not in CustomFieldType.values:
        raise ContactsError("That is not a field type.")
    _assert_name_is_free(CustomField, workspace, cleaned, noun="field")
    with _unique_name("field"):
        return CustomField.objects.create(workspace=workspace, name=cleaned, type=field_type)


def rename_custom_field(field: CustomField, name: str) -> CustomField:
    """Rename a field. The **type is immutable** and there is deliberately no
    setter for it.

    Flipping ``text`` to ``number`` would orphan every existing value in
    ``value_text``, where the condition engine — which reads the column named by
    the *new* type — sees "no value". The row-level check constraint cannot spot
    it either, because each row remains internally consistent. A retype that
    migrates or drops values belongs to issue #13, with a preview of what it
    will discard.
    """
    cleaned = _clean_name(name, limit=100, noun="field")
    _assert_name_is_free(CustomField, field.workspace_id, cleaned, noun="field", excluding=field.pk)
    field.name = cleaned
    with _unique_name("field"):
        field.save(update_fields=["name", "updated_at"])
    return field


def delete_custom_field(field: CustomField) -> int:
    """Delete a field and every stored value. Returns how many values went.

    Sends no ``contact.field_changed`` per contact, for the reason
    :func:`delete_tag` gives.
    """
    # Live contacts, not stored rows — see delete_tag for why `.delete()`'s
    # cascade count answers a different question than the one the operator asked.
    live = (
        CustomFieldValue.objects.for_workspace(field.workspace_id)
        .filter(field=field, contact__status=ContactStatus.ACTIVE)
        .count()
    )
    field.delete()
    return live


def coerce_value(field: CustomField, value: Any) -> tuple[str, Any]:
    """``(column, coerced value)`` for a write, or raise :class:`FieldTypeError`.

    The one type gate in the app. Every rejection is deliberate:

    * ``bool`` is not a number, even though ``isinstance(True, int)`` is ``True``
      in Python — without the check, ``True`` would quietly store as ``1``.
    * ``NaN`` and infinities are rejected. Postgres ``numeric`` accepts NaN
      happily, and a stored NaN falsifies *every* comparison in the SPEC §11.4
      operator table, so the value would be invisible to the filter that went
      looking for it.
    * A ``datetime`` for a ``date`` field is rejected rather than truncated:
      silently dropping the time is the kind of data loss that surfaces a month
      later.
    * A naive ``datetime`` is rejected rather than localised. ``USE_TZ`` is on,
      and localising into the workspace's zone would make the same input mean
      two things in two workspaces. The caller localises.
    """
    column = VALUE_COLUMNS[field.type]

    if field.type == CustomFieldType.BOOLEAN:
        if not isinstance(value, bool):
            raise FieldTypeError(f"{field.name} holds true or false.")
        return column, value

    if field.type == CustomFieldType.NUMBER:
        if isinstance(value, bool) or not isinstance(value, int | float | str | Decimal):
            raise FieldTypeError(f"{field.name} holds a number.")
        try:
            number = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise FieldTypeError(f"{field.name} holds a number.") from exc
        if not number.is_finite():
            raise FieldTypeError(f"{field.name} holds a number.")
        # is_finite() above already excluded NaN and the infinities, whose
        # exponent is a letter rather than an int; the isinstance keeps the
        # type checker in step with that.
        exponent = number.as_tuple().exponent
        if (isinstance(exponent, int) and exponent < -6) or abs(number) >= Decimal(10) ** 14:
            raise FieldTypeError(f"{field.name} holds a number with at most 14 digits and 6 decimal places.")
        return column, number

    if field.type == CustomFieldType.DATE:
        if isinstance(value, datetime):
            raise FieldTypeError(f"{field.name} holds a date without a time.")
        if isinstance(value, date):
            return column, value
        if isinstance(value, str):
            try:
                return column, date.fromisoformat(value)
            except ValueError as exc:
                raise FieldTypeError(f"{field.name} holds a date like 2026-08-21.") from exc
        raise FieldTypeError(f"{field.name} holds a date.")

    if field.type == CustomFieldType.DATETIME:
        moment = value
        if isinstance(moment, str):
            try:
                moment = datetime.fromisoformat(moment)
            except ValueError as exc:
                raise FieldTypeError(f"{field.name} holds a date and time with a UTC offset.") from exc
        if not isinstance(moment, datetime):
            raise FieldTypeError(f"{field.name} holds a date and time.")
        if timezone.is_naive(moment):
            raise FieldTypeError(f"{field.name} holds a date and time with a UTC offset.")
        return column, moment

    if not isinstance(value, str):
        raise FieldTypeError(f"{field.name} holds text.")
    if len(value) > MAX_TEXT_VALUE_CHARS:
        raise FieldTypeError(f"{field.name} holds at most {MAX_TEXT_VALUE_CHARS} characters.")
    if "\x00" in value:
        raise FieldTypeError(f"{field.name} cannot hold a null byte.")
    return column, value.strip()


def set_field_value(contact: Contact, field: CustomField, value: Any) -> CustomFieldValue:
    """Store ``value`` for ``field`` on ``contact``, typed.

    Writes all five typed columns, four of them ``None``: the check constraint
    is "exactly one populated", so a partial write over a row that previously
    held a different type would be an ``IntegrityError`` — a 500 rather than a
    validation error.

    Sends ``contact.field_changed`` only when the stored value actually changed.
    """
    if field.workspace_id != contact.workspace_id:
        raise WorkspaceMismatchError("That field belongs to a different workspace than the contact.")
    column, coerced = coerce_value(field, value)
    columns: dict[str, Any] = dict.fromkeys(VALUE_COLUMNS.values())
    columns[column] = coerced

    rows = CustomFieldValue.objects.for_workspace(contact.workspace_id)
    with transaction.atomic():
        existing = rows.filter(contact=contact, field=field).first()
        changed = existing is None or getattr(existing, column) != coerced
        # update_or_create rather than a hand-rolled read-then-write: two workers
        # setting the same previously-unset field both see `existing is None` and
        # both insert, and the loser hits the (contact, field) unique constraint.
        # Django's implementation absorbs exactly that conflict and re-reads,
        # which turns a routine automation race into an applied write instead of
        # a failed request.
        #
        # `defaults` carries all five typed columns, four of them None: the check
        # constraint is "exactly one populated", so a partial write over a row
        # that previously held another type would be an IntegrityError.
        row, _created = rows.update_or_create(contact=contact, field=field, defaults=columns)
        if changed:
            emit(
                EVENT_CONTACT_FIELD_CHANGED,
                workspace_id=contact.workspace_id,
                contact_id=contact.pk,
                field_id=field.pk,
                cleared=False,
            )
    return row


def clear_field_value(contact: Contact, field: CustomField) -> bool:
    """Remove ``field``'s value from ``contact``. ``True`` when one existed.

    Deletes the row rather than nulling the columns, which is what keeps the
    check constraint's "exactly one populated" true rather than "at most one".
    """
    rows = CustomFieldValue.objects.for_workspace(contact.workspace_id).filter(contact=contact, field=field)
    with transaction.atomic():
        removed, _ = rows.delete()
        if not removed:
            return False
        emit(
            EVENT_CONTACT_FIELD_CHANGED,
            workspace_id=contact.workspace_id,
            contact_id=contact.pk,
            field_id=field.pk,
            cleared=True,
        )
    return True


def field_values_for(contact: Contact) -> dict[UUID, Any]:
    """``{custom_field_id: value}`` for one contact."""
    rows = CustomFieldValue.objects.for_workspace(contact.workspace_id).filter(contact=contact).select_related("field")
    return {row.field_id: row.value for row in rows}


# ---------------------------------------------------------------------------
# Segments
# ---------------------------------------------------------------------------


def _as_filter_document(filter_json: Any) -> dict[str, Any]:
    """Require a real JSON object for a stored filter.

    ``conditions.validate()`` accepts a raw string and parses it, which is right
    for an API boundary — but storing that string in a ``JSONField`` would leave
    the column holding a JSON *string* rather than an object, so every later read
    re-parses it and the shape of what is on disk depends on how it was written.
    Segments keep the parsed form.
    """
    if not isinstance(filter_json, dict):
        raise ContactsError("A segment filter must be a JSON object.")
    return filter_json


def create_segment(workspace: Any, *, name: str, filter_json: Any) -> Segment:
    """Create a segment, validating its filter first.

    ``Model.clean()`` does not run on ``.save()``, so the programmatic path
    needs its own gate — the admin gets one for free through ``full_clean()``.
    """
    from apps.contacts.conditions import validate

    cleaned = _clean_name(name, limit=100, noun="segment")
    document = _as_filter_document(filter_json)
    _assert_name_is_free(Segment, workspace, cleaned, noun="segment")
    validate(workspace, document)
    with _unique_name("segment"):
        return Segment.objects.create(workspace=workspace, name=cleaned, filter_json=document)


def update_segment(segment: Segment, *, name: str | None = None, filter_json: Any = None) -> Segment:
    from apps.contacts.conditions import validate

    changed: list[str] = []
    if name is not None:
        cleaned = _clean_name(name, limit=100, noun="segment")
        _assert_name_is_free(Segment, segment.workspace_id, cleaned, noun="segment", excluding=segment.pk)
        segment.name = cleaned
        changed.append("name")
    if filter_json is not None:
        document = _as_filter_document(filter_json)
        validate(segment.workspace_id, document, exclude_segment_id=segment.pk)
        segment.filter_json = document
        changed.append("filter_json")
    if changed:
        with _unique_name("segment"):
            segment.save(update_fields=[*changed, "updated_at"])
    return segment
