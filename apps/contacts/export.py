"""Streaming a filtered contact list out as CSV (SPEC §2: contacts owns export).

The whole file is one generator handed to ``StreamingHttpResponse``, so a
workspace with fifty thousand contacts costs a bounded amount of memory and the
browser starts receiving rows before the query has finished. Building the
document in a list first would put the entire export in the web process's heap —
the failure the import path is batched to avoid, arriving from the other
direction.

--------------------------------------------------------------------------
Formula injection
--------------------------------------------------------------------------

A spreadsheet is an execution context. Excel, LibreOffice and Sheets all treat a
cell beginning ``=``, ``+``, ``-`` or ``@`` as a formula, and
``=HYPERLINK("https://…"&A1)`` in a contact's first name exfiltrates the column
beside it the moment somebody opens the export. Contact names, emails and custom
field values arrive from strangers (SECURITY-BASELINE §2), so every cell goes
through :func:`escape_cell`.

Prefixing with an apostrophe rather than stripping the character: a phone number
really can start with ``+``, and a note really can start with ``-``. The
apostrophe is the spreadsheet convention for "this is text", it is not part of
the value, and the alternative loses data to defend against a payload.
"""

import csv
from collections.abc import Iterable, Iterator, Sequence
from typing import Any

from django.db.models import QuerySet
from django.utils import timezone

from apps.contacts.models import Contact, CustomField, CustomFieldValue, Tag

__all__ = ["EXPORT_COLUMNS", "csv_stream", "escape_cell", "export_filename", "stream_contacts"]

#: The system columns, in order. ``id`` leads so an export can be used as the
#: input to a later re-import that updates rather than duplicates.
EXPORT_COLUMNS: tuple[str, ...] = (
    "id",
    "first_name",
    "last_name",
    "email",
    "phone",
    "locale",
    "timezone",
    "created_at",
    "last_interaction_at",
    "tags",
)

#: Characters that make a spreadsheet treat a cell as a formula. The two control
#: characters are here because a leading tab or CR shifts the value into the
#: next cell, where a payload can start with one of the other four.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

#: Rows fetched per round trip. Large enough that a 50k export is a few hundred
#: queries rather than tens of thousands, small enough to stay bounded.
CHUNK_SIZE = 2000

#: Semicolon, not comma: a comma-joined tag list inside a CSV cell is correct but
#: unreadable in the quoted form every writer produces for it, and tag names
#: legitimately contain commas.
TAG_SEPARATOR = "; "


class _Echo:
    """A file-like object whose ``write`` returns the line instead of storing it.

    Django's documented idiom for streaming CSV: ``csv.writer`` insists on
    something with ``write``, and this turns each ``writerow`` call into a string
    the generator can yield.
    """

    def write(self, value: str) -> str:
        return value


def csv_stream(rows: Iterable[Sequence[Any]]) -> Iterator[str]:
    """Render rows of arbitrary values as CSV lines, every cell neutralised.

    The single place :func:`escape_cell` is applied, so a second CSV surface
    cannot ship without the formula guard by forgetting to call it — the import
    error report is that second surface, and it quotes the values that caused the
    errors, which is exactly the untrusted text this module exists to defuse.
    """
    writer = csv.writer(_Echo())
    for row in rows:
        yield writer.writerow([escape_cell(cell) for cell in row])


def escape_cell(value: Any) -> str:
    """Render one value as CSV text, neutralised against formula injection.

    ``None`` becomes an empty cell rather than the string ``None``: a re-import
    of this file must read "no value", and a literal ``None`` in an email column
    is a value.
    """
    if value is None:
        return ""
    if value is True or value is False:
        # Before the str() below, and spelled with `is`: bool is an int subclass,
        # so a plain isinstance check against int would catch it too, and "True"
        # is what a re-import's boolean coercion expects rather than "1".
        return "true" if value else "false"
    text = str(value)
    if text.startswith(_FORMULA_PREFIXES):
        return f"'{text}"
    return text


def export_filename(workspace: Any) -> str:
    """``contacts-<workspace>-<date>.csv``, built only from values we chose.

    The workspace name is deliberately **not** in it. It is operator-supplied
    text heading for a ``Content-Disposition`` header, where a newline is header
    injection and a quote ends the filename parameter early.
    """
    return f"contacts-{workspace.pk}-{timezone.localdate().isoformat()}.csv"


def _custom_field_columns(workspace: Any) -> list[CustomField]:
    return list(CustomField.objects.for_workspace(workspace).order_by("name"))


def _tags_by_contact(workspace: Any, contact_ids: list[Any]) -> dict[Any, list[str]]:
    rows = (
        Tag.objects.for_workspace(workspace)
        .filter(contact_tags__contact_id__in=contact_ids)
        .values_list("contact_tags__contact_id", "name")
        .order_by("name")
    )
    found: dict[Any, list[str]] = {}
    for contact_id, name in rows:
        found.setdefault(contact_id, []).append(name)
    return found


def _values_by_contact(workspace: Any, contact_ids: list[Any]) -> dict[Any, dict[Any, Any]]:
    rows = CustomFieldValue.objects.for_workspace(workspace).filter(contact_id__in=contact_ids).select_related("field")
    found: dict[Any, dict[Any, Any]] = {}
    for row in rows:
        found.setdefault(row.contact_id, {})[row.field_id] = row.value
    return found


def _chunks(rows: QuerySet[Contact]) -> Iterator[list[Contact]]:
    """Slice the queryset into pages of :data:`CHUNK_SIZE`.

    ``.iterator()`` would be the obvious tool and is the wrong one here: the tag
    and custom-field lookups below are per-chunk batch queries keyed on the ids
    in hand, so the generator needs the ids as a **list** before it can ask for
    them. Slicing an ordered queryset gives exactly that, and the ordering is
    total (every entry in ``filters.SORTS`` ends with the primary key), so
    consecutive pages cannot repeat or skip a row.
    """
    offset = 0
    while True:
        page = list(rows[offset : offset + CHUNK_SIZE])
        if not page:
            return
        yield page
        if len(page) < CHUNK_SIZE:
            return
        offset += CHUNK_SIZE


def stream_contacts(workspace: Any, rows: QuerySet[Contact]) -> Iterator[str]:
    """Yield the export a line at a time: header, then every matching contact.

    Custom fields become one column each, named after the field, so the file is
    readable by a human and re-importable by the mapping step without a nested
    encoding to explain. A workspace with a hundred custom fields gets a hundred
    columns, which is what it asked for.
    """
    yield from csv_stream(_records(workspace, rows))


def _records(workspace: Any, rows: QuerySet[Contact]) -> Iterator[list[Any]]:
    """The header, then one list per contact. Raw values — escaping is
    :func:`csv_stream`'s single responsibility."""
    fields = _custom_field_columns(workspace)
    yield [*EXPORT_COLUMNS, *(field.name for field in fields)]

    for page in _chunks(rows):
        ids = [contact.pk for contact in page]
        tags = _tags_by_contact(workspace, ids)
        values = _values_by_contact(workspace, ids)
        for contact in page:
            record = values.get(contact.pk, {})
            yield [
                contact.pk,
                contact.first_name,
                contact.last_name,
                contact.email,
                contact.phone,
                contact.locale,
                contact.timezone,
                contact.created_at.isoformat() if contact.created_at else "",
                contact.last_interaction_at.isoformat() if contact.last_interaction_at else "",
                TAG_SEPARATOR.join(tags.get(contact.pk, [])),
                *(record.get(field.pk) for field in fields),
            ]
