"""``/api/v1/contacts`` — SPEC §17's contact surface.

**Every write goes through** ``apps.contacts.services`` (ROADMAP contract 1).
Not one field on one model is assigned here. That is not style: the contract-7
events outbound webhooks consume are emitted *by* those services, so an API that
wrote ``contact.first_name = …`` would update the CRM and deliver nothing, and
the divergence would show up as "webhooks are flaky" months later.

Permission gates mirror the equivalent page in the CRM UI rather than inventing
a second policy — reads on ``use_inbox`` (every role has it), scalar and tag
edits on ``edit_contact_fields`` (``contact_edit``, ``contact_tag_add``), and
creating or starting a flow on ``manage_crm`` (``contact_start_flow``).
"""

import datetime as dt
import json
from typing import Annotated, Any
from uuid import UUID

from django.db import transaction
from django.db.models import QuerySet
from django.http import Http404
from django.utils import timezone
from ninja import Query, Router, Status

from apps.api.errors import ApiError
from apps.api.pagination import paginate
from apps.api.requests import ApiRequest
from apps.api.schemas import (
    ContactCreate,
    ContactOut,
    ContactUpdate,
    FieldValueIn,
    FieldValueOut,
    FlowStartIn,
    FlowStartOut,
    Page,
    TagAdd,
    TagOut,
)
from apps.api.serializers import contact_payload, field_value_payload, tag_payload
from apps.common.jsonlimits import max_json_depth
from apps.common.shortcuts import get_scoped_object_or_404
from apps.contacts import filters as contact_filters
from apps.contacts import services as contact_services
from apps.contacts.errors import ContactsError
from apps.contacts.models import Contact, ContactStatus, CustomField, CustomFieldValue, Tag
from apps.flows.models import Flow
from apps.members.decorators import require_permission

router = Router(tags=["contacts"])

#: Cap on the ``variables`` blob a flow start may carry (SECURITY-BASELINE §7).
#: Flow variables are rendered into messages, so this is untrusted content on
#: its way to a template context, and "as much JSON as the body cap allows" is
#: not a bound anyone reasoned about.
MAX_VARIABLES_BYTES = 8 * 1024
MAX_VARIABLES_DEPTH = 6


def _contact_or_404(request: ApiRequest, contact_id: UUID) -> Contact:
    """Fetch a contact in the key's workspace, or 404.

    A deleted contact answers 404 as well: soft deletion is invisible from
    outside, and the API must not become the one surface that reports it.
    """
    contact = get_scoped_object_or_404(Contact, request.workspace, pk=contact_id)
    if contact.status != ContactStatus.ACTIVE:
        raise Http404("No such contact.")
    return contact


def _page(queryset: Any, *, limit: int | None, cursor: str | None, render: Any) -> dict[str, Any]:
    try:
        page = paginate(queryset, limit=limit, cursor=cursor)
    except ValueError as exc:
        raise ApiError(str(exc), code="invalid_cursor", status=422) from exc
    return {**page, "data": [render(row) for row in page["data"]]}


def _aware(moment: dt.datetime | None) -> dt.datetime | None:
    """Treat a naive timestamp in a query string as UTC rather than rejecting it."""
    if moment is None or timezone.is_aware(moment):
        return moment
    return timezone.make_aware(moment, dt.UTC)


@router.get("/contacts", response=Page[ContactOut], url_name="contacts_list")
@require_permission("use_inbox")
def list_contacts(
    request: ApiRequest,
    q: Annotated[str, Query(description="Case-insensitive match on name, email or phone.")] = "",
    tag_id: Annotated[UUID | None, Query(description="Only contacts carrying this tag.")] = None,
    status: Annotated[str, Query(description='"active" or "deleted".')] = "active",
    created_after: Annotated[dt.datetime | None, Query()] = None,
    created_before: Annotated[dt.datetime | None, Query()] = None,
    updated_after: Annotated[dt.datetime | None, Query()] = None,
    limit: Annotated[int | None, Query(description="1-200, default 50.")] = None,
    cursor: Annotated[str | None, Query(description="Opaque; echo back next_cursor.")] = None,
) -> dict[str, Any]:
    """List contacts, newest first.

    ``?q=`` reuses ``apps.contacts.filters.search`` rather than spelling the
    four ``icontains`` clauses again — the CRM list, the CSV export and this
    endpoint have to agree on what "search" means.
    """
    if status not in set(ContactStatus.values):
        raise ApiError(
            f"status must be one of {', '.join(sorted(ContactStatus.values))}.",
            code="invalid_request",
            status=422,
        )
    rows: QuerySet[Contact] = Contact.objects.for_workspace(request.workspace).filter(status=status)
    if q:
        rows = contact_filters.search(rows, q)
    if tag_id is not None:
        rows = rows.filter(tags__id=tag_id)
    if created_after is not None:
        rows = rows.filter(created_at__gte=_aware(created_after))
    if created_before is not None:
        rows = rows.filter(created_at__lt=_aware(created_before))
    if updated_after is not None:
        rows = rows.filter(updated_at__gte=_aware(updated_after))
    # A stable tiebreak, because offset pagination is only correct with one.
    rows = rows.prefetch_related("tags").order_by("-created_at", "-id")
    return _page(rows, limit=limit, cursor=cursor, render=contact_payload)


@router.post("/contacts", response={201: ContactOut}, url_name="contacts_create")
@require_permission("manage_crm")
def create_contact(request: ApiRequest, payload: ContactCreate) -> Status[dict[str, Any]]:
    """Create a contact with ``source="api"`` (SPEC §5's consent audit)."""
    try:
        contact = contact_services.create_contact(
            request.workspace,
            first_name=payload.first_name,
            last_name=payload.last_name,
            email=payload.email,
            phone=payload.phone,
            locale=payload.locale,
            contact_timezone=payload.timezone,
            source="api",
        )
    except ContactsError as exc:
        raise ApiError(str(exc), code="invalid_request", status=422) from exc
    return Status(201, contact_payload(contact))


@router.get("/contacts/{uuid:contact_id}", response=ContactOut, url_name="contacts_detail")
@require_permission("use_inbox")
def get_contact(request: ApiRequest, contact_id: UUID) -> dict[str, Any]:
    return contact_payload(_contact_or_404(request, contact_id))


@router.patch("/contacts/{uuid:contact_id}", response=ContactOut, url_name="contacts_update")
@require_permission("edit_contact_fields")
def update_contact(request: ApiRequest, contact_id: UUID, payload: ContactUpdate) -> dict[str, Any]:
    """Partial update. Omitted fields are left alone; ``""`` clears one."""
    contact = _contact_or_404(request, contact_id)
    changes = {key: value for key, value in payload.dict(exclude_unset=True).items() if value is not None}
    if not changes:
        return contact_payload(contact)
    try:
        contact_services.update_contact(contact, **changes)
    except ContactsError as exc:
        raise ApiError(str(exc), code="invalid_request", status=422) from exc
    contact.refresh_from_db()
    return contact_payload(contact)


@router.post("/contacts/{uuid:contact_id}/tags", response={200: TagOut, 201: TagOut}, url_name="contacts_tag_add")
@require_permission("edit_contact_fields")
def add_contact_tag(request: ApiRequest, contact_id: UUID, payload: TagAdd) -> Status[dict[str, Any]]:
    """Attach a tag, by name or by id.

    By name creates the tag when the workspace does not have it — the same
    ``get_or_create_tag`` the CRM's tag box calls. 201 when the link was newly
    made, 200 when it was already there, so a retrying integration can tell an
    idempotent no-op from a change.
    """
    if bool(payload.name) == bool(payload.tag_id):
        raise ApiError("Provide exactly one of name or tag_id.", code="invalid_request", status=422)

    contact = _contact_or_404(request, contact_id)
    if payload.tag_id is not None:
        tag = get_scoped_object_or_404(Tag, request.workspace, pk=payload.tag_id)
    else:
        try:
            tag, _ = contact_services.get_or_create_tag(request.workspace, payload.name or "")
        except ContactsError as exc:
            raise ApiError(str(exc), code="invalid_request", status=422) from exc

    try:
        added = contact_services.add_tag(contact, tag)
    except ContactsError as exc:
        raise ApiError(str(exc), code="invalid_request", status=422) from exc
    return Status(201 if added else 200, tag_payload(tag))


@router.delete(
    "/contacts/{uuid:contact_id}/tags/{uuid:tag_id}",
    response={204: None},
    url_name="contacts_tag_remove",
)
@require_permission("edit_contact_fields")
def remove_contact_tag(request: ApiRequest, contact_id: UUID, tag_id: UUID) -> Status[None]:
    """Detach a tag. 204 whether or not it was attached — the end state is the same."""
    contact = _contact_or_404(request, contact_id)
    tag = get_scoped_object_or_404(Tag, request.workspace, pk=tag_id)
    contact_services.remove_tag(contact, tag)
    return Status(204, None)


@router.put(
    "/contacts/{uuid:contact_id}/fields/{uuid:field_id}",
    response=FieldValueOut,
    url_name="contacts_field_set",
)
@require_permission("edit_contact_fields")
def set_contact_field(request: ApiRequest, contact_id: UUID, field_id: UUID, payload: FieldValueIn) -> dict[str, Any]:
    """Set or clear one custom field.

    Typing is ``apps.contacts.services.coerce_value``'s job — the field's own
    type decides what a value may be, and a second opinion here would be a
    second, disagreeing type system. A null clears the value.
    """
    contact = _contact_or_404(request, contact_id)
    field = get_scoped_object_or_404(CustomField, request.workspace, pk=field_id)
    try:
        if payload.value is None:
            contact_services.clear_field_value(contact, field)
            return field_value_payload(field, None)
        row = contact_services.set_field_value(contact, field, payload.value)
    except ContactsError as exc:
        raise ApiError(str(exc), code="invalid_field_value", status=422) from exc
    return field_value_payload(field, row.value)


@router.get("/contacts/{uuid:contact_id}/fields", response=list[FieldValueOut], url_name="contacts_field_list")
@require_permission("use_inbox")
def list_contact_fields(request: ApiRequest, contact_id: UUID) -> list[dict[str, Any]]:
    """Every custom field value set on this contact."""
    contact = _contact_or_404(request, contact_id)
    rows = (
        CustomFieldValue.objects.for_workspace(request.workspace)
        .filter(contact=contact)
        .select_related("field")
        .order_by("field__name")
    )
    return [field_value_payload(row.field, row.value) for row in rows]


def _validated_variables(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Bound the caller-supplied variables blob (SECURITY-BASELINE §7)."""
    if not raw:
        return {}
    encoded = json.dumps(raw, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_VARIABLES_BYTES:
        raise ApiError(
            f"variables must serialise to at most {MAX_VARIABLES_BYTES} bytes.",
            code="invalid_request",
            status=422,
        )
    if max_json_depth(encoded) > MAX_VARIABLES_DEPTH:
        raise ApiError("variables is nested too deeply.", code="invalid_request", status=422)
    return raw


@router.post(
    "/contacts/{uuid:contact_id}/flows/{uuid:flow_id}/start",
    response={202: FlowStartOut},
    url_name="contacts_flow_start",
)
@require_permission("manage_crm")
def start_flow_for_contact(
    request: ApiRequest,
    contact_id: UUID,
    flow_id: UUID,
    payload: FlowStartIn | None = None,
) -> Status[dict[str, Any]]:
    """Fire the flow's ``api`` trigger (SPEC §10).

    Goes through ``apps.flows.triggers.entrypoints.fire_api_trigger``, which
    L4-A shipped for this endpoint and says so in its own docstring. It owns the
    contact lock, supersede semantics and the cross-workspace refusal — calling
    ``engine.start_flow`` directly would start the flow without the trigger,
    which is the one thing SPEC §10's ``api`` type exists to record.
    """
    from apps.flows.triggers.entrypoints import fire_api_trigger

    options = payload or FlowStartIn()
    contact = _contact_or_404(request, contact_id)
    flow = get_scoped_object_or_404(Flow, request.workspace, pk=flow_id)
    connection = None
    if options.connection_id is not None:
        from apps.channels.models import ChannelConnection

        connection = get_scoped_object_or_404(ChannelConnection, request.workspace, pk=options.connection_id)

    variables = _validated_variables(options.variables)
    with transaction.atomic():
        result = fire_api_trigger(
            flow=flow,
            contact=contact,
            key=options.trigger_key,
            variables=variables,
            connection=connection,
        )

    if result.started and result.execution is not None:
        return Status(
            202,
            {
                "execution_id": result.execution.pk,
                "flow_id": flow.pk,
                "contact_id": contact.pk,
                "status": result.execution.status,
            },
        )
    if result.reason == "lock_contention":
        return Status(
            202,
            {"execution_id": None, "flow_id": flow.pk, "contact_id": contact.pk, "status": "queued"},
        )
    if result.reason == "no_api_trigger":
        raise ApiError(
            "This flow has no enabled api trigger.",
            code="no_api_trigger",
            status=422,
        )
    if result.reason == "cross_workspace":
        # Unreachable through this route — both objects were scoped — but a 404
        # rather than a 500 is the right answer if it ever becomes reachable.
        raise Http404("No such flow.")
    raise ApiError("This flow cannot be started.", code="flow_not_runnable", status=422)
