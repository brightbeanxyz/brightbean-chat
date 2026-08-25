"""Request and response shapes for ``/api/v1/``.

Two conventions, applied without exception:

**Input schemas forbid unknown keys.** ``extra="forbid"`` is SECURITY-BASELINE
§7's mass-assignment guard in one line: a caller cannot smuggle ``workspace`` or
``status`` into a create by adding a key nobody validated, and a typo in a field
name is a 422 rather than a silently ignored value. Every input schema below
inherits :class:`StrictSchema` for it.

**Output schemas are explicit.** No ``ModelSchema``, no ``fields="__all__"``:
what a public API returns is a contract, and a contract that changes shape when
someone adds a column is not one. Adding a field here is a deliberate act.

Every list response is the same envelope — ``data``, ``has_more``,
``next_cursor`` — built by :mod:`apps.api.pagination`.

No ``from __future__ import annotations`` here or in the routers, deliberately.
Pydantic resolves these annotations at runtime, and it does so in *its* module
namespace rather than in ours — so postponed (string) annotations leave it
unable to find names that are plainly imported at the top of this file, and the
failure surfaces as a 500 from ``model_rebuild`` rather than as an import error.
Python 3.12 needs none of it for ``X | None`` anyway.
"""

import datetime as dt
from typing import Any
from uuid import UUID

from ninja import Schema
from pydantic import ConfigDict

__all__ = [
    "ContactCreate",
    "ContactOut",
    "ContactUpdate",
    "CustomFieldOut",
    "FieldValueIn",
    "FieldValueOut",
    "FlowOut",
    "FlowStartIn",
    "FlowStartOut",
    "MessageOut",
    "MessageSend",
    "Page",
    "StrictSchema",
    "TagAdd",
    "TagOut",
]


class StrictSchema(Schema):
    """Base for every request body. Unknown keys are an error, not a shrug."""

    model_config = ConfigDict(extra="forbid")


class Page[T](Schema):
    """The one list envelope.

    No ``total``: counting a workspace's contacts is the expensive half of a
    list request and nothing in the documented contract promises it. Page
    forward with ``next_cursor`` until ``has_more`` is false.
    """

    data: list[T]
    has_more: bool
    next_cursor: str | None = None


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------


class TagOut(Schema):
    id: UUID
    name: str


class ContactOut(Schema):
    id: UUID
    first_name: str
    last_name: str
    email: str
    phone: str
    locale: str
    timezone: str
    status: str
    tags: list[TagOut] = []
    last_interaction_at: dt.datetime | None = None
    created_at: dt.datetime
    updated_at: dt.datetime


class ContactCreate(StrictSchema):
    """Everything ``apps.contacts.services.create_contact`` accepts, and nothing else.

    ``source`` is not a field: an object created through this API is
    ``source="api"``, which is already one of the values
    ``apps.contacts.services.CONTACT_SOURCES`` allows, and letting a caller
    claim a different provenance would make the consent audit (SPEC §11.8)
    fiction.
    """

    first_name: str = ""
    last_name: str = ""
    email: str = ""
    phone: str = ""
    locale: str = ""
    timezone: str = ""


class ContactUpdate(StrictSchema):
    """A partial update. Every field is optional; omitted fields are untouched.

    Deliberately **not** nullable. The underlying columns default to ``""`` and
    cannot hold NULL, so ``{"email": null}`` has no meaning here — and typing
    these as ``str | None`` would have advertised nullability in the generated
    OpenAPI document while the route quietly dropped the null, which is the
    worst of both. Send ``""`` to clear a field; send ``null`` and you get a 422
    that says so.
    """

    first_name: str = ""
    last_name: str = ""
    email: str = ""
    phone: str = ""
    locale: str = ""
    timezone: str = ""


class TagAdd(StrictSchema):
    """Add a tag by name or by id. Exactly one of the two.

    By name is the useful form for an integration that does not keep our ids;
    the tag is created if the workspace does not have it yet, which is what
    ``get_or_create_tag`` does for the CRM UI. The response carries the id, and
    removing a tag is by id — a name can contain anything, including a slash.
    """

    name: str | None = None
    tag_id: UUID | None = None


# ---------------------------------------------------------------------------
# Custom fields
# ---------------------------------------------------------------------------


class CustomFieldOut(Schema):
    id: UUID
    name: str
    type: str


class FieldValueIn(StrictSchema):
    """Set one custom field. ``null`` clears it.

    ``value`` is deliberately untyped here: the field's own type decides what is
    acceptable, and ``apps.contacts.services.coerce_value`` is the one gate that
    decides. A schema that also had an opinion would be a second, disagreeing
    type system.
    """

    value: Any = None


class FieldValueOut(Schema):
    field_id: UUID
    name: str
    type: str
    value: Any = None


# ---------------------------------------------------------------------------
# Flows
# ---------------------------------------------------------------------------


class FlowOut(Schema):
    id: UUID
    name: str
    status: str
    folder: str
    created_at: dt.datetime
    updated_at: dt.datetime


class FlowStartIn(StrictSchema):
    """Options for firing the ``api`` trigger.

    ``trigger_key`` selects among several ``api`` triggers on the same flow —
    it is matched against the trigger's ``config_json["key"]``. Omit it and the
    lowest-priority enabled ``api`` trigger wins.
    """

    trigger_key: str = ""
    variables: dict[str, Any] | None = None
    connection_id: UUID | None = None


class ErasureOut(Schema):
    """The receipt for a GDPR erasure (SPEC §19, issue #29).

    Returned with ``202`` when the work was handed to a worker, and pollable at
    ``GET /erasures/{id}`` until ``status`` reads ``done``. A ``202`` with no way
    to check the outcome would make "did it happen?" unanswerable over the API,
    which is the opposite of what an audit trail is for.

    ``counts`` is a row count per model label. It carries no personal data —
    that is the point of it being counts.
    """

    id: UUID
    contact_id: UUID
    status: str
    counts: dict[str, int] = {}
    created_at: dt.datetime
    completed_at: dt.datetime | None = None


class FlowStartOut(Schema):
    """The outcome of firing an ``api`` trigger.

    ``execution_id`` is null in exactly one case: another event held the
    contact's advisory lock, so the start was queued instead of run inline
    (``status`` is then ``"queued"``). SPEC §9.6 serialises everything one
    contact does behind that lock, and waiting on it inside an HTTP request
    would trade a fast 202 for an unbounded one.
    """

    execution_id: UUID | None = None
    flow_id: UUID
    contact_id: UUID
    status: str


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


class MessageBody(StrictSchema):
    """SPEC §17's ``body``. Text only in v1.

    The normalized body (SPEC §7.2) can carry media, cards and buttons, and the
    flow builder is how those get authored. Widening this is a later,
    deliberate change; a public API that can post a card carousel is a public
    API whose capability-downgrade behaviour has to be documented per platform.
    """

    text: str


class MessageSend(StrictSchema):
    contact_id: UUID
    connection_id: UUID
    body: MessageBody
    idempotency_key: str | None = None


class MessageOut(Schema):
    id: UUID
    conversation_id: UUID
    contact_id: UUID
    status: str
    source: str
    error: str = ""
    created_at: dt.datetime
