"""Contact, tag and custom-field pages, all gated on ``manage_crm``.

The contact list is deliberately small: issue #13 (L4-C) owns the CRM — detail
pages, inline editing, search, CSV import and export — and this issue needs only
enough to replace the placeholder the shell has been linking to. What it does
add beyond a plain list is a segment selector, because that makes the condition
engine's set-wise path (ROADMAP contract 8) visible in the product rather than
only in a test, and gives issue #13 somewhere to start.

Tags and custom fields, by contrast, are finished here: their settings pages are
this issue's, not #13's.

Decorator order is the house convention — ``@login_required`` →
``@require_permission`` → ``@require_GET``/``@require_POST`` — and the method
check being innermost is load-bearing: a cross-tenant GET must answer 404 from
the middleware rather than 405 from the method guard, which is what the IDOR
sweep's "at least one method answers 404" contract relies on.

Mutations answer with :func:`apps.common.htmx.toast_response` — a 204 carrying
``HX-Trigger`` — and the list container re-fetches itself on the event. Failures
are **also 204**, deliberately: htmx does not process ``HX-Trigger`` on a
non-2xx response by default, so answering 400 would swallow the very message the
user needs to read.
"""

from typing import Any

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, QuerySet
from django.http import HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from apps.common.htmx import toast_response
from apps.common.shortcuts import get_scoped_object_or_404
from apps.contacts import services
from apps.contacts.conditions import ConditionError
from apps.contacts.conditions import queryset as contacts_matching
from apps.contacts.errors import ContactsError
from apps.contacts.models import Contact, ContactStatus, CustomField, CustomFieldType, Segment, Tag
from apps.members.decorators import require_permission
from apps.members.requests import WorkspaceRequest

PAGE_SIZE = 50


def _failed(exc: Exception, title: str) -> HttpResponse:
    return toast_response(tone="error", title=title, body=str(exc))


@login_required
@require_permission("manage_crm")
@require_GET
def contact_list(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """Read-only, paginated. Optionally narrowed to a saved segment.

    The ``?segment=`` id is a **query parameter**, so ``tests/idor.py`` — which
    walks URL kwargs — cannot see it. It goes through
    ``get_scoped_object_or_404`` and has its own cross-tenant test in
    ``tests/test_views.py``; this is exactly the class of gap the sweep is blind
    to, and it is called out in the PR for that reason.
    """
    segments = list(Segment.objects.for_workspace(request.workspace))
    contacts: QuerySet[Contact] = Contact.objects.for_workspace(request.workspace).filter(status=ContactStatus.ACTIVE)

    selected = request.GET.get("segment") or ""
    error = ""
    if selected:
        segment = get_scoped_object_or_404(Segment, request.workspace, pk=selected)
        try:
            contacts = contacts_matching(request.workspace, segment.filter_json)
        except ConditionError as exc:
            # A segment saved before a tag it references was deleted, or one
            # using a source this deployment cannot evaluate yet. Show the list
            # unfiltered and say why rather than 500.
            error = str(exc)

    # -id, not -created_at, as the tiebreak: primary keys are UUIDv7, so it is a
    # stable, unique, index-backed newest-first ordering that costs nothing.
    # prefetch_related, or the tag column is one query per row.
    rows = contacts.prefetch_related("tags").order_by("-last_interaction_at", "-id")
    page = Paginator(rows, PAGE_SIZE).get_page(request.GET.get("page"))
    return render(
        request,
        "contacts/list.html",
        {"page": page, "segments": segments, "selected_segment": selected, "segment_error": error},
    )


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------


@login_required
@require_permission("manage_crm")
@require_GET
def tag_list(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    tags = Tag.objects.for_workspace(request.workspace).annotate(contact_count=Count("contacts"))
    return render(request, "contacts/tag_list.html", {"tags": tags})


@login_required
@require_permission("manage_crm")
@require_POST
def tag_create(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    try:
        tag, created = services.get_or_create_tag(request.workspace, request.POST.get("name", ""))
    except ContactsError as exc:
        return _failed(exc, "Could not create the tag")
    if not created:
        return toast_response(tone="info", title="That tag already exists", body=tag.name)
    return toast_response(tone="success", title="Tag created", body=tag.name, events={"tagsChanged": True})


@login_required
@require_permission("manage_crm")
@require_POST
def tag_rename(request: WorkspaceRequest, workspace_id: str, tag_id: str) -> HttpResponse:
    tag = get_scoped_object_or_404(Tag, request.workspace, pk=tag_id)
    try:
        services.rename_tag(tag, request.POST.get("name", ""))
    except ContactsError as exc:
        return _failed(exc, "Could not rename the tag")
    return toast_response(tone="success", title="Tag renamed", body=tag.name, events={"tagsChanged": True})


@login_required
@require_permission("manage_crm")
@require_POST
def tag_delete(request: WorkspaceRequest, workspace_id: str, tag_id: str) -> HttpResponse:
    tag = get_scoped_object_or_404(Tag, request.workspace, pk=tag_id)
    name = tag.name
    removed = services.delete_tag(tag)
    return toast_response(
        tone="success",
        title="Tag deleted",
        body=f"{name} — removed from {removed} contact{'' if removed == 1 else 's'}.",
        events={"tagsChanged": True},
    )


# ---------------------------------------------------------------------------
# Custom fields
# ---------------------------------------------------------------------------


@login_required
@require_permission("manage_crm")
@require_GET
def field_list(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    fields = CustomField.objects.for_workspace(request.workspace).annotate(value_count=Count("values"))
    context: dict[str, Any] = {"fields": fields, "field_types": CustomFieldType.choices}
    return render(request, "contacts/field_list.html", context)


@login_required
@require_permission("manage_crm")
@require_POST
def field_create(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    try:
        field = services.create_custom_field(
            request.workspace,
            name=request.POST.get("name", ""),
            field_type=request.POST.get("field_type", ""),
        )
    except ContactsError as exc:
        return _failed(exc, "Could not create the field")
    return toast_response(tone="success", title="Field created", body=field.name, events={"fieldsChanged": True})


@login_required
@require_permission("manage_crm")
@require_POST
def field_rename(request: WorkspaceRequest, workspace_id: str, field_id: str) -> HttpResponse:
    """Rename only — a field's type is immutable. See ``services.rename_custom_field``."""
    field = get_scoped_object_or_404(CustomField, request.workspace, pk=field_id)
    try:
        services.rename_custom_field(field, request.POST.get("name", ""))
    except ContactsError as exc:
        return _failed(exc, "Could not rename the field")
    return toast_response(tone="success", title="Field renamed", body=field.name, events={"fieldsChanged": True})


@login_required
@require_permission("manage_crm")
@require_POST
def field_delete(request: WorkspaceRequest, workspace_id: str, field_id: str) -> HttpResponse:
    field = get_scoped_object_or_404(CustomField, request.workspace, pk=field_id)
    name = field.name
    removed = services.delete_custom_field(field)
    return toast_response(
        tone="success",
        title="Field deleted",
        body=f"{name} — {removed} stored value{'' if removed == 1 else 's'} removed.",
        events={"fieldsChanged": True},
    )
