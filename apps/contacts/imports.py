"""CSV import: parsing, the dry run, and the batched queue handler (SPEC §2, §15).

The issue's acceptance criterion is "50k-row CSV imports in background without
web-request timeouts, dry-run catches type errors, report downloadable". Three
properties fall out of that, and everything here is shaped by them.

--------------------------------------------------------------------------
1. One loop, two modes
--------------------------------------------------------------------------

The dry run and the import are the **same code path** with ``mode`` deciding
whether :func:`_apply_row` writes. A separate validator would be a second
implementation of "what does this row mean", and the failure of a disagreement
between them is the worst kind available here: a preview that says the file is
clean and an import that then half-applies it.

--------------------------------------------------------------------------
2. Batched, because a handler runs inside a transaction
--------------------------------------------------------------------------

``apps.queueing.worker`` runs each handler inside ``transaction.atomic()``. A
50 000-row import as one action would therefore be one transaction holding tens
of thousands of new rows, and one crash away from doing all of it again. So each
action processes :data:`~django.conf.settings.CONTACT_IMPORT_BATCH_ROWS` rows
from ``next_offset``, writes the counters, and re-schedules itself for the next
slice **in the same transaction** — so a batch and the arrangement to continue
it commit together, or neither does.

That is also what makes a crash safe: a batch that dies rolls back its rows
*and* its counter update, so the retry starts from the same ``next_offset`` and
cannot double-create.

The scheduled row deliberately carries **no contact**. ``apps.queueing.worker``
takes the contact advisory lock when one is named (SPEC §9.6's one-step-per-
contact invariant), and bulk work that holds a contact lock for the length of a
batch would stall every flow for that contact behind it.

--------------------------------------------------------------------------
3. No identity is ever fabricated
--------------------------------------------------------------------------

An imported contact is **not reachable on any channel**. This module never calls
``upsert_contact_identity``: a spreadsheet column is not consent, and a phone
number typed into a CRM is not a WhatsApp opt-in. The contact exists, is
segmentable and is visible in the CRM; the moment they message a connected
channel, ingest creates the identity with a real consent record (SPEC §11.8).
The consent checkbox on the upload step says exactly this, and
``ContactImport.consent_ack`` records that it was shown.

Rows go through :mod:`apps.contacts.services` one at a time rather than
``bulk_create``. That costs queries and buys two things the shortcut loses:
contract 7's ``contact.created`` / ``contact.tag_added`` events fire, and
``ContactScopedModel.save()`` derives ``workspace`` — which ``bulk_create``
skips, as ``apps/contacts/models.py`` warns in as many words.
"""

import csv
import io
import logging
from dataclasses import dataclass, field
from itertools import islice
from typing import Any
from uuid import UUID

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone

from apps.contacts.errors import ContactsError
from apps.contacts.models import (
    FINISHED_IMPORT_STATUSES,
    Contact,
    ContactImport,
    ContactStatus,
    CustomField,
    ImportDedupe,
    ImportStatus,
)
from apps.queueing.registry import register_handler, schedule

logger = logging.getLogger(__name__)

__all__ = [
    "ACTION_TYPE",
    "IMPORTABLE_SYSTEM_FIELDS",
    "MODE_DRY_RUN",
    "MODE_IMPORT",
    "ResolvedMapping",
    "RowError",
    "enqueue",
    "handle_contact_import",
    "preview",
    "read_header",
    "resolve_mapping",
]

#: The queue action type. ``apps.queueing.models.ActionType`` is deliberately an
#: open set (its docstring says so) precisely so a later app can claim a name
#: without an ``AlterField`` migration in that app.
ACTION_TYPE = "contact_import"

MODE_DRY_RUN = "dry_run"
MODE_IMPORT = "import"

#: The mapping's ``system:`` targets. The same six columns
#: ``apps.contacts.services.update_contact`` accepts — an allowlist, so a
#: mapping naming ``status`` or ``workspace`` is refused rather than applied.
IMPORTABLE_SYSTEM_FIELDS: tuple[str, ...] = (
    "first_name",
    "last_name",
    "email",
    "phone",
    "locale",
    "timezone",
)

#: The special target for the column holding a contact's tags.
TAGS_TARGET = "tags"

#: Separators accepted inside a tags cell. Both, because an operator exporting
#: from another CRM has no way to know which one this app wanted.
TAG_SPLIT_CHARS = ";,"

#: ``Tag.name``'s column width. Mirrored rather than imported because
#: ``services.get_or_create_tag`` takes it as a literal too; the dry run has to
#: know it to refuse a row the import would refuse.
MAX_TAG_NAME_CHARS = 100

#: Delimiters the header sniffer will consider, in preference order.
CANDIDATE_DELIMITERS = (",", ";", "\t", "|")

#: Cap on one row's total text. A single cell holding ten megabytes is not a
#: contact, and the byte cap on the upload bounds the file rather than the row.
MAX_ROW_CHARS = 100_000

#: Cap on how wide a file may be. A thousand columns is already absurd for a
#: contact list, and the mapping UI renders one control per column.
MAX_COLUMNS = 200


class UnusableImportError(ContactsError):
    """The file or the mapping cannot be used at all — as opposed to one bad row."""


@dataclass(frozen=True)
class RowError:
    """One row's refusal, as stored in ``ContactImport.errors``.

    ``row`` is 1-based over **data** rows, matching what the operator sees when
    they open the file with a header: row 1 of the report is the first line under
    the headings.
    """

    row: int
    column: str
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {"row": self.row, "column": self.column, "message": self.message}


@dataclass
class ResolvedMapping:
    """A mapping checked against the workspace as it is *now*.

    Re-resolved on every batch rather than trusted from the row that stored it: a
    mapping is a document that has been sitting in a table, possibly since before
    someone deleted the custom field it names. That is the same reasoning
    ``apps/flows/handlers.py`` gives for re-resolving a scheduled action's ids.
    """

    #: column index -> contact attribute name
    system: dict[int, str] = field(default_factory=dict)
    #: column index -> CustomField
    fields: dict[int, CustomField] = field(default_factory=dict)
    #: column index holding tags, or None
    tags_column: int | None = None

    @property
    def is_empty(self) -> bool:
        return not self.system and not self.fields and self.tags_column is None

    @property
    def match_field(self) -> str:
        """Which column dedupe matches on: email if mapped, else phone, else "".

        Email first because it is the identifier operators actually deduplicate
        on, and because ``create_contact`` lowercases it, so the ``(workspace,
        email)`` index answers the probe as an equality rather than a scan.
        """
        mapped = set(self.system.values())
        if "email" in mapped:
            return "email"
        return "phone" if "phone" in mapped else ""


# ---------------------------------------------------------------------------
# Reading the file
# ---------------------------------------------------------------------------


def _decoded(run: ContactImport) -> io.StringIO:
    """The whole upload as text, decoded defensively.

    Read in full rather than streamed: the upload is capped at
    ``CONTACT_IMPORT_MAX_BYTES`` (20 MB by default), which is a bounded amount of
    memory in a worker, and the alternative — a ``TextIOWrapper`` over a storage
    file — behaves differently on S3 and on local disk and would have to be
    re-opened per batch anyway.

    ``utf-8-sig`` first, because a file saved by Excel begins with a BOM and
    ``utf-8`` would read it as part of the first column's name — which silently
    breaks the mapping for that one column only. ``cp1252`` is the fallback
    because it is what Excel on Windows writes and because it cannot fail: every
    byte sequence decodes, so an unknown encoding degrades to mojibake in a value
    rather than an unimportable file.
    """
    if not run.file:
        # The retention sweep drops a finished run's file (SPEC §19), and the
        # upload path deletes the row when the file turns out to be unreadable.
        # Either way this is a run that cannot proceed, not a transient fault:
        # raising a plain OSError here would spend SPEC §15's five-attempt
        # backoff ladder re-discovering that a deleted file is still deleted.
        raise UnusableImportError("That file is no longer available.")
    try:
        with run.file.open("rb") as handle:
            raw = handle.read()
    except (OSError, ValueError) as exc:
        raise UnusableImportError("That file could not be read.") from exc
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", errors="replace")
    return io.StringIO(text, newline="")


def _delimiter(sample: str) -> str:
    """Pick the delimiter by counting candidates in the header line.

    ``csv.Sniffer`` is the obvious tool and guesses badly on the files that
    matter here: a single-column export has no delimiter to find and it raises,
    and a header holding an address ("Smith, J") makes it confident about the
    wrong one. Counting on the header alone is cruder and predictable, and the
    mapping step shows the operator the columns it found before anything is
    imported.
    """
    line = sample.splitlines()[0] if sample else ""
    best = max(CANDIDATE_DELIMITERS, key=line.count)
    return best if line.count(best) else ","


def _reader(run: ContactImport) -> tuple[Any, list[str]]:
    """``(csv reader positioned after the header, header)``."""
    buffer = _decoded(run)
    head = buffer.read(64 * 1024)
    buffer.seek(0)
    reader = csv.reader(buffer, delimiter=_delimiter(head))
    try:
        header = next(reader)
    except StopIteration as exc:
        raise UnusableImportError("That file is empty.") from exc
    if len(header) > MAX_COLUMNS:
        raise UnusableImportError(f"That file has more than {MAX_COLUMNS} columns.")
    return reader, [name.strip() for name in header]


def read_header(run: ContactImport) -> list[str]:
    """The file's column names, as the mapping step lists them.

    Read from the file every time rather than stored on the row: the file does
    not change, so there is nothing for a copy to be right about that this is
    not, and one fewer column is one fewer thing that can go stale.
    """
    _, header = _reader(run)
    return header


def preview(run: ContactImport, limit: int | None = None) -> list[list[str]]:
    """The first few data rows, for the mapping step's sample table.

    Synchronous, in the request, which is why it is bounded by
    ``CONTACT_IMPORT_PREVIEW_ROWS`` and why the *full* check is the queued dry
    run. Seeing three real rows under the column headings is what stops an
    operator mapping "Surname" onto ``first_name``.
    """
    reader, _ = _reader(run)
    count = limit or settings.CONTACT_IMPORT_PREVIEW_ROWS
    return [row for row in islice(reader, count)]


# ---------------------------------------------------------------------------
# The mapping
# ---------------------------------------------------------------------------


def resolve_mapping(workspace: Any, mapping: Any, header: list[str]) -> ResolvedMapping:
    """Check a stored mapping against this workspace and this file.

    Keys are **column indexes** rather than column names. A CSV may legitimately
    repeat a heading, and a name-keyed mapping silently collapses the duplicates
    into whichever one it saw last — mapping data from a column the operator
    never chose. The header is only ever a label.

    Refusals are :class:`UnusableImportError`, because a mapping that no longer resolves
    means the whole run cannot proceed: unlike a bad row, retrying it row by row
    produces the same answer fifty thousand times.
    """
    if not isinstance(mapping, dict):
        raise UnusableImportError("That import has no column mapping yet.")

    resolved = ResolvedMapping()
    field_ids: dict[int, UUID] = {}
    for raw_key, raw_target in mapping.items():
        index = _column_index(raw_key, header)
        if index is None or not raw_target:
            continue
        target = str(raw_target)
        if target == TAGS_TARGET:
            resolved.tags_column = index
        elif target.startswith("system:"):
            name = target.removeprefix("system:")
            if name not in IMPORTABLE_SYSTEM_FIELDS:
                raise UnusableImportError(f"{name!r} is not an importable contact field.")
            resolved.system[index] = name
        elif target.startswith("field:"):
            try:
                field_ids[index] = UUID(target.removeprefix("field:"))
            except ValueError as exc:
                raise UnusableImportError("That mapping names a custom field that does not exist.") from exc
        else:
            raise UnusableImportError(f"{target!r} is not a column target.")

    if field_ids:
        # One query for every custom field the mapping names, scoped — so
        # another workspace's field id is simply absent and reads as "deleted"
        # rather than as a cross-tenant write.
        found = {
            row.pk: row for row in CustomField.objects.for_workspace(workspace).filter(pk__in=set(field_ids.values()))
        }
        for index, field_id in field_ids.items():
            row = found.get(field_id)
            if row is None:
                raise UnusableImportError("That mapping names a custom field that has been deleted.")
            resolved.fields[index] = row

    if len(set(resolved.system.values())) != len(resolved.system):
        raise UnusableImportError("Two columns are mapped to the same contact field.")
    if resolved.is_empty:
        raise UnusableImportError("Map at least one column before importing.")
    return resolved


def _column_index(raw_key: Any, header: list[str]) -> int | None:
    try:
        index = int(raw_key)
    except (TypeError, ValueError):
        return None
    return index if 0 <= index < len(header) else None


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------


def enqueue(run: ContactImport, *, mode: str, offset: int = 0) -> Any:
    """Schedule one batch. Returns the queue row.

    **Deliberately no idempotency key.** The obvious one —
    ``contact_import:<run>:<mode>:<offset>`` — is wrong twice over. It would make
    re-checking a file after fixing its mapping a silent no-op, because the key
    for offset 0 is already in the table from the first attempt and ``schedule``
    answers "already arranged" whatever that row's status. And it would not
    actually stop the thing it looks like it stops: two workers claiming two
    batch-0 rows at once both read ``next_offset = 0`` and both write.

    What does stop it is the row lock :func:`_run_batch` takes on the
    ``ContactImport`` before it reads that counter, so a second batch for the same
    run waits, re-reads, and finds itself stale. Correctness lives there, in one
    place, rather than being split between a lock and a key that only covers the
    single-worker case.
    """
    return schedule(
        ACTION_TYPE,
        timezone.now(),
        {"import_id": str(run.pk), "mode": mode, "offset": offset},
        # The instance, not the id: ``schedule`` assigns it straight onto the
        # ScheduledAction's FK, which refuses a bare UUID.
        workspace=run.workspace,
        # No contact: see the module docstring on the advisory lock.
    )


# ---------------------------------------------------------------------------
# The handler
# ---------------------------------------------------------------------------


@register_handler(ACTION_TYPE)
def handle_contact_import(payload: dict[str, Any], action: Any) -> None:
    """Process one batch of a CSV import, then arrange the next.

    Failures divide in two, and the division is the interesting part:

    * A **row** failure is data. It is recorded in the report and the batch
      carries on, because five retries over six hours cannot make a malformed
      email address parse, and a run that stopped at the first bad row would make
      an operator fix a fifty-thousand-line file one line per attempt.
    * A **run** failure — the file is gone, the mapping names a deleted field —
      marks the import failed and returns. Also not retriable, and raising would
      spend the backoff ladder re-reading a file that is not there.

    Everything else propagates, so a genuinely unexpected error rolls the batch
    back and retries on SPEC §15's ladder.
    """
    mode = payload.get("mode") or MODE_DRY_RUN
    offset = int(payload.get("offset") or 0)

    # Nested inside the worker's own transaction (a savepoint), and required
    # rather than tidy: the row lock below is what serialises two batches for the
    # same import, and a lock outside a transaction is not a lock.
    with transaction.atomic():
        run = _locked_run(action.workspace_id, payload.get("import_id"))
        if run is None:
            logger.warning("contact_import action %s names an import that is gone; dropping it.", action.pk)
            return
        if run.status in FINISHED_IMPORT_STATUSES:
            logger.info("Import %s is already %s; ignoring a stale batch.", run.pk, run.status)
            return
        if offset != run.next_offset:
            # A duplicate delivery, a double-clicked button, or a batch whose
            # successor already ran. The counters are the authority on where this
            # run has got to; the payload is only a request.
            logger.info("Import %s: batch at offset %s is stale (next is %s).", run.pk, offset, run.next_offset)
            return

        try:
            _run_batch(run, mode=mode, offset=offset)
        except UnusableImportError as exc:
            _fail(run, str(exc))


def _locked_run(workspace_id: Any, import_id: Any) -> ContactImport | None:
    """The run, locked for the length of this batch.

    ``select_for_update`` on the import row is the whole concurrency story:
    ``next_offset`` is read and advanced under it, so two workers processing two
    batch rows for the same import cannot both decide they are batch zero. The
    contact advisory lock the queue takes for contact-scoped work is deliberately
    not in play — these rows name no contact, precisely so a bulk import does not
    hold one (SPEC §9.6).
    """
    if not import_id:
        return None
    try:
        return ContactImport.objects.for_workspace(workspace_id).select_for_update().filter(pk=import_id).first()
    except (ValidationError, ValueError, TypeError):
        return None


def _run_batch(run: ContactImport, *, mode: str, offset: int) -> None:
    reader, header = _reader(run)
    mapping = resolve_mapping(run.workspace_id, run.mapping, header)
    batch_size = settings.CONTACT_IMPORT_BATCH_ROWS
    max_rows = settings.CONTACT_IMPORT_MAX_ROWS

    if offset == 0:
        _begin(run, mode)

    errors: list[RowError] = []
    counts = {"created": 0, "updated": 0, "skipped": 0}
    processed = 0
    #: Set when the row cap stopped this batch early. The rest of the file is
    #: deliberately not read: the cap is what bounds the whole run's cost.
    capped = False

    for index, row in enumerate(islice(reader, offset, offset + batch_size), start=offset):
        processed += 1
        number = index + 1
        if number > max_rows:
            errors.append(RowError(number, "", f"This file has more than {max_rows:,} rows; the rest were skipped."))
            capped = True
            break
        if sum(len(cell) for cell in row) > MAX_ROW_CHARS:
            errors.append(RowError(number, "", "That row is too long to import."))
            continue
        try:
            outcome = _apply_row(run, mapping, row, number, write=mode == MODE_IMPORT)
        except ContactsError as exc:
            # A refusal from the service layer that the row checks did not
            # anticipate — a name over the column width, a NUL byte. Data, not a
            # bug: recorded and stepped over, like every other bad row.
            errors.append(RowError(number, "", str(exc)))
            continue
        if isinstance(outcome, RowError):
            errors.append(outcome)
            continue
        counts[outcome] += 1

    _record_batch(run, mode=mode, processed=processed, counts=counts, errors=errors)

    # A short batch is the end of the file: `islice` stopped because the reader
    # did. A full one means there may be more, so the run continues — and the
    # re-schedule happens inside the worker's transaction, so this batch and the
    # arrangement to continue it commit together or not at all.
    if capped or processed < batch_size:
        _finish(run, mode)
    else:
        enqueue(run, mode=mode, offset=offset + processed)


def _begin(run: ContactImport, mode: str) -> None:
    """Reset the counters at the start of a pass.

    A dry run and the import that follows it are two passes over the same file,
    and the second must not add its counts to the first's — an operator who saw
    "40 will be created" then "80 created" would have no way to tell which number
    was wrong.
    """
    run.status = ImportStatus.VALIDATING if mode == MODE_DRY_RUN else ImportStatus.IMPORTING
    run.processed_rows = 0
    run.created_count = 0
    run.updated_count = 0
    run.skipped_count = 0
    run.error_count = 0
    run.errors = []
    run.finished_at = None
    run.save(
        update_fields=[
            "status",
            "processed_rows",
            "created_count",
            "updated_count",
            "skipped_count",
            "error_count",
            "errors",
            "finished_at",
            "updated_at",
        ]
    )


def _record_batch(
    run: ContactImport,
    *,
    mode: str,
    processed: int,
    counts: dict[str, int],
    errors: list[RowError],
) -> None:
    run.processed_rows += processed
    run.next_offset += processed
    run.created_count += counts["created"]
    run.updated_count += counts["updated"]
    run.skipped_count += counts["skipped"]
    run.error_count += len(errors)
    if mode == MODE_DRY_RUN:
        # The dry run is the pass that learns how long the file is; the import
        # then has a denominator for its progress bar from the first batch.
        run.total_rows = run.processed_rows
    room = ContactImport.MAX_REPORTED_ROW_ERRORS - len(run.errors)
    if room > 0:
        run.errors = [*run.errors, *(error.as_dict() for error in errors[:room])]
    run.save(
        update_fields=[
            "processed_rows",
            "next_offset",
            "created_count",
            "updated_count",
            "skipped_count",
            "error_count",
            "errors",
            "total_rows",
            "updated_at",
        ]
    )


def _finish(run: ContactImport, mode: str) -> None:
    run.status = ImportStatus.VALIDATED if mode == MODE_DRY_RUN else ImportStatus.DONE
    run.next_offset = 0
    run.finished_at = timezone.now()
    run.save(update_fields=["status", "next_offset", "finished_at", "updated_at"])
    logger.info(
        "Import %s finished %s: %s created, %s updated, %s skipped, %s errors.",
        run.pk,
        mode,
        run.created_count,
        run.updated_count,
        run.skipped_count,
        run.error_count,
    )


def _fail(run: ContactImport, message: str) -> None:
    run.status = ImportStatus.FAILED
    run.finished_at = timezone.now()
    room = ContactImport.MAX_REPORTED_ROW_ERRORS - len(run.errors)
    if room > 0:
        run.errors = [*run.errors, RowError(0, "", message).as_dict()]
    run.error_count += 1
    run.save(update_fields=["status", "finished_at", "errors", "error_count", "updated_at"])
    logger.warning("Import %s failed: %s", run.pk, message)


# ---------------------------------------------------------------------------
# One row
# ---------------------------------------------------------------------------


def _apply_row(
    run: ContactImport,
    mapping: ResolvedMapping,
    row: list[str],
    number: int,
    *,
    write: bool,
) -> str | RowError:
    """Validate one row and, when ``write``, apply it. Returns the outcome name.

    The dry run reaches every check here — the type coercion in particular, which
    is where a "Signed up" column holding ``last Tuesday`` fails — and simply
    returns before the writes. That is the "one loop, two modes" property the
    module docstring opens with, and it is why the preview's promise is worth
    anything.
    """
    from apps.contacts import services

    values = {name: _cell(row, index) for index, name in mapping.system.items()}
    if error := _validate_system(values, number, row, mapping):
        return error

    typed: list[tuple[CustomField, Any]] = []
    for index, custom in mapping.fields.items():
        raw = _cell(row, index)
        if not raw:
            continue
        try:
            services.coerce_value(custom, raw)
        except ContactsError as exc:
            return RowError(number, custom.name, str(exc))
        typed.append((custom, raw))

    names = _tag_names(row, mapping)
    for name in names:
        # Checked in BOTH passes, before the `write` early-return below. A tag
        # name over the column width raises from get_or_create_tag during the
        # import, and a dry run that skipped the check would promise a clean file
        # and then report the failure only after half of it had been written.
        if len(name) > MAX_TAG_NAME_CHARS:
            return RowError(number, TAGS_TARGET, f"A tag name is at most {MAX_TAG_NAME_CHARS} characters.")

    existing = _match(run.workspace_id, values, mapping) if mapping.match_field else None

    if existing is not None and run.dedupe == ImportDedupe.SKIP:
        return "skipped"
    if not write:
        return "updated" if existing is not None and run.dedupe == ImportDedupe.UPDATE else "created"

    if existing is not None and run.dedupe == ImportDedupe.UPDATE:
        contact = existing
        # Blank cells do not clear a stored value. A partial export re-imported
        # to fill in phone numbers must not wipe every name it left out, and
        # "clear this field" is an edit somebody makes on the detail page.
        services.update_contact(contact, **{name: value for name, value in values.items() if value})
        outcome = "updated"
    else:
        contact = services.create_contact(
            run.workspace,
            source="import",
            first_name=values.get("first_name", ""),
            last_name=values.get("last_name", ""),
            email=values.get("email", ""),
            phone=values.get("phone", ""),
            locale=values.get("locale", ""),
            # Spelled `contact_timezone` at the call site because `timezone`
            # there would shadow ``django.utils.timezone``; the column is
            # ``timezone``.
            contact_timezone=values.get("timezone", ""),
        )
        outcome = "created"

    for custom, raw in typed:
        services.set_field_value(contact, custom, raw)
    for name in names:
        tag, _created = services.get_or_create_tag(run.workspace, name)
        services.add_tag(contact, tag)
    return outcome


def _cell(row: list[str], index: int) -> str:
    return row[index].strip() if index < len(row) else ""


def _validate_system(values: dict[str, str], number: int, row: list[str], mapping: ResolvedMapping) -> RowError | None:
    """Refuse a row before anything is written, or return ``None``.

    Only email is format-checked. SPEC §5 is explicit that a contact's phone is
    "whatever the platform or the operator supplied" and is not checked for
    deliverability, and rejecting a locale this app does not recognise would
    refuse rows over a column nothing reads yet. Email is the exception because
    it is the dedupe key: a malformed one silently matches nothing, so every row
    carrying it would create a duplicate on the next import of the same file.
    """
    email = values.get("email", "")
    if email:
        try:
            validate_email(email)
        except ValidationError:
            return RowError(number, "email", "That is not an email address.")
    mapped = [*mapping.system, *mapping.fields, *([mapping.tags_column] if mapping.tags_column is not None else [])]
    if not any(_cell(row, index) for index in mapped):
        # Trailing blank lines are ordinary in a hand-edited export, so this is a
        # skip rather than an error — an operator should not have to read a
        # report telling them their file ended.
        return RowError(number, "", "That row is empty.")
    return None


def _tag_names(row: list[str], mapping: ResolvedMapping) -> list[str]:
    if mapping.tags_column is None:
        return []
    raw = _cell(row, mapping.tags_column)
    for separator in TAG_SPLIT_CHARS:
        raw = raw.replace(separator, "\n")
    # De-duplicated case-insensitively, in the order the cell lists them.
    # ``get_or_create_tag`` matches case-insensitively too, so "VIP;vip" would
    # otherwise be two round trips adding the same link twice — and the second
    # would return False, making the row look like it did less than it did.
    ordered: list[str] = []
    taken: set[str] = set()
    for name in raw.split("\n"):
        cleaned = " ".join(name.split())
        if cleaned and cleaned.casefold() not in taken:
            taken.add(cleaned.casefold())
            ordered.append(cleaned)
    return ordered


def _match(workspace_id: Any, values: dict[str, str], mapping: ResolvedMapping) -> Contact | None:
    """The existing contact this row names, by email or phone.

    Active contacts only. A soft-deleted contact is a tombstone every read
    surface has stopped showing, and quietly resurrecting one by updating it from
    an import would put somebody back into a send path without anybody choosing
    that — so an import that names a deleted contact creates a new one, which is
    the same answer the operator would get if they typed it in by hand.
    """
    key = mapping.match_field
    value = values.get(key, "")
    if not value:
        return None
    if key == "email":
        value = value.lower()
    return (
        Contact.objects.for_workspace(workspace_id)
        .filter(status=ContactStatus.ACTIVE, **{key: value})
        .order_by("created_at")
        .first()
    )
