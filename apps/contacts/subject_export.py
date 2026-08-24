"""The subject access response: everything this workspace holds about one person.

SPEC §19 asks for "export = JSON dump endpoint on contact view", and issue #29
spells out what has to be in it — profile, fields, tags, identities **including
consent records**, message history, execution history, enrollment history.

Separate from :mod:`apps.contacts.export`, which is the CRM's CSV of *many*
contacts, streamed and formula-escaped for a spreadsheet. This is one contact,
rendered as JSON for a person exercising a right rather than for Excel, so it
shares neither the column list nor the escaping — a JSON string needs no
apostrophe in front of a leading ``=``.

--------------------------------------------------------------------------
Three things it does that a smaller version would not
--------------------------------------------------------------------------

**Consent is first-class.** ``opt_in_at``, ``opt_in_source`` and
``opted_out_at`` (SPEC §11.8) are the columns a regulator asks about, and an
export that listed a contact's channels without saying when and how they agreed
to be messaged would answer the easy half of the question.

**It discloses what erasure will keep.** The email suppression list is keyed on
the mailbox and deliberately has no foreign key to a contact, so it survives a
hard delete (``apps/channels/models.py`` argues why). Article 15 is about what
the controller holds, not about what is convenient to list, so it is in the
document — and ``retained`` says in words why.

**It admits what it left out.** ``truncated`` and ``not_included`` are part of
the schema rather than a footnote in the docs. A document that looks complete
and is not is worse than one that names its own edges.

--------------------------------------------------------------------------
A build trap worth knowing about
--------------------------------------------------------------------------

``apps/messaging/tests/test_write_sites.py`` scans every non-test module under
``apps/`` and records the keyword arguments of any ``.update()``, ``.create()``,
``.get_or_create()`` or ``.update_or_create()`` call **without looking at what
it was called on**. So ``document.update(opted_out_at=...)`` on a plain dict
fails the build exactly as an ORM write to that pinned column would. Everything
here is built with dict literals and subscripts, and it stays that way.
"""

from decimal import Decimal
from typing import Any

from django.conf import settings
from django.utils import timezone

from apps.contacts import activity
from apps.contacts.models import Contact, CustomFieldValue

__all__ = ["SCHEMA", "VERSION", "build", "filename"]

#: Identifies the document, the way ``apps.flows.schema.envelope`` identifies an
#: exported flow. A consumer that reads one of these in a year should not have
#: to guess what shape it is looking at.
SCHEMA = "brightbean.contact_export"
VERSION = 1

#: What this document deliberately does not contain, in words, because the
#: honest answer to "is this everything?" is "everything but these, and here is
#: why". Rendered into the payload, not just into this docstring.
NOT_INCLUDED: tuple[dict[str, str], ...] = (
    {
        "category": "Raw webhook payloads",
        "detail": (
            "Inbound deliveries are logged verbatim for replay protection and debugging. The log is keyed on the "
            "channel connection and the platform's event id, with no reference to a contact, so it cannot be "
            "searched by person. It is pruned automatically 30 days after receipt."
        ),
    },
    {
        "category": "CSV import files",
        "detail": (
            "A spreadsheet uploaded to the importer is stored with the import run and quotes whatever cells it "
            "contained. Nothing links a row of it back to the contact it created. Files are pruned automatically "
            "after the workspace's import retention window."
        ),
    },
)


def build(contact: Contact) -> dict[str, Any]:
    """The whole document, as plain JSON-safe data.

    One dict, built once and returned — no streaming. A single contact's history
    is bounded by :data:`~django.conf.settings.CONTACT_EXPORT_MAX_MESSAGES`,
    which is what makes that safe; the CSV export streams because it is
    unbounded in the number of *contacts*, which is a different problem.
    """
    conversations, truncated = activity.conversation_history(contact, limit=settings.CONTACT_EXPORT_MAX_MESSAGES)
    suppressions = activity.suppressions_for(contact)

    return {
        "schema": SCHEMA,
        "version": VERSION,
        "generated_at": timezone.now().isoformat(),
        "workspace": {"id": str(contact.workspace_id), "name": contact.workspace.name},
        "contact": _profile(contact),
        "custom_fields": _custom_fields(contact),
        "tags": _tags(contact),
        # SPEC §11.8's consent audit. The reason this reader exists rather than
        # the CRM's ContactChannel dataclass being reused: see activity.py.
        "identities": activity.consent_records(contact),
        "conversations": conversations,
        "executions": activity.executions_for(contact),
        "enrollments": activity.enrollments_for(contact),
        "broadcasts": activity.broadcast_receipts_for(contact),
        "retained": _retained(suppressions),
        "truncated": {"messages": truncated},
        "not_included": list(NOT_INCLUDED),
    }


def filename(contact: Contact) -> str:
    """``contact-<id>-<date>.json``.

    The id rather than the name: this file is about to be attached to a support
    ticket or an email, and a filename is the one part of a document that gets
    read by people who were never meant to see the contents.
    """
    return f"contact-{contact.pk}-{timezone.now():%Y-%m-%d}.json"


def _profile(contact: Contact) -> dict[str, Any]:
    return {
        "id": str(contact.pk),
        "first_name": contact.first_name,
        "last_name": contact.last_name,
        "email": contact.email,
        "phone": contact.phone,
        "locale": contact.locale,
        "timezone": contact.timezone,
        "status": contact.status,
        "created_at": contact.created_at.isoformat(),
        "updated_at": contact.updated_at.isoformat(),
        "last_interaction_at": (
            contact.last_interaction_at.isoformat() if contact.last_interaction_at is not None else None
        ),
    }


def _custom_fields(contact: Contact) -> list[dict[str, Any]]:
    """Stored field values, typed as they were written.

    ``Decimal`` is unwrapped to a JSON number for the reason
    ``apps.api.serializers.field_value_payload`` gives: a value that goes in as
    ``42`` and comes out as ``"42"`` is an asymmetric contract. Re-spelled here
    rather than imported, because ``apps.contacts`` importing ``apps.api`` would
    invert a dependency that runs the other way.
    """
    rows = (
        CustomFieldValue.objects.for_workspace(contact.workspace_id)
        .filter(contact=contact)
        .select_related("field")
        .order_by("field__name")
    )
    values = []
    for row in rows:
        value = row.value
        if isinstance(value, Decimal):
            value = int(value) if value == value.to_integral_value() else float(value)
        elif hasattr(value, "isoformat"):
            value = value.isoformat()
        values.append({"field_id": str(row.field_id), "name": row.field.name, "type": row.field.type, "value": value})
    return values


def _tags(contact: Contact) -> list[dict[str, Any]]:
    """Through the join rows, never ``contact.tags``.

    Two reasons. The relation is declared read-only and its ``m2m_changed``
    receiver raises on mutation, so reading through it invites a later edit that
    does not; and the link row carries ``created_at``, which is *when* the
    person was labelled — part of what was held about them.
    """
    links = contact.contact_tags.select_related("tag").order_by("tag__name")
    return [{"id": str(link.tag_id), "name": link.tag.name, "added_at": link.created_at.isoformat()} for link in links]


def _retained(suppressions: list[dict[str, Any]]) -> dict[str, Any]:
    """What survives an erasure, and the reason, stated rather than implied."""
    return {
        "email_suppressions": suppressions,
        "note": (
            "A suppressed email address is kept even after this contact is erased. The record is a fact about a "
            "mailbox — that it rejected mail or its owner reported it as spam — rather than about a contact row, "
            "and deleting it would mean a later import could mail an address that asked not to be mailed."
        ),
    }
