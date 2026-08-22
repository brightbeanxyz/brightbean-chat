"""The CRM: the contact list, the contact detail page, segments and CSV
import/export — plus the tag and custom-field settings pages.

Two issues meet in this module. L2-A (#3) shipped the tag and field settings
pages finished, and a deliberately small contact list whose only job was to put
the condition engine's set-wise path in front of a human. L4-C (#13) turns that
list into the product surface and adds everything around it. The settings pages
below are the originals, extended with tag merge; **there is exactly one tag
editor in this app**, and adding a second one beside it was the named failure
mode for this issue.

--------------------------------------------------------------------------
Who may do what
--------------------------------------------------------------------------

Reading the CRM is ``@require_workspace_role(WorkspaceRole.VIEWER)`` — "is a
member of this workspace" — rather than a permission key, and that is a
considered choice. SPEC §4's table has nine keys and none of them means "may see
contacts": an Agent has ``edit_contact_fields`` and a Viewer has neither that nor
``manage_crm``, yet the issue's acceptance criteria require an Agent to edit a
contact's tags and fields and a Viewer to see the CRM read-only. Adding a tenth
key would diverge from a table the SPEC writes out in full, and reusing
``use_inbox`` would gate the Contacts page on a key that means something else.
CONTRIBUTING.md's preference for ``require_permission`` holds where the gate is a
named capability; this one is seniority, which is the case that decorator
documents itself as being for.

Writes keep using keys, and split at the line SPEC §4 already draws:

* ``edit_contact_fields`` (agent+) — edit a contact's system fields and custom
  field values, attach and detach **existing** tags, opt an identity out.
* ``manage_crm`` (editor+) — everything that changes the workspace's shape or
  moves data in bulk: creating a tag, segments, delete, import, export, starting
  and stopping automation, and the two settings pages.

Creating a tag from the detail page's typeahead is tag CRUD, so it is
``manage_crm``; *attaching* one an Agent can already see is not. The typeahead
offers its "create" affordance only to holders of the former.

--------------------------------------------------------------------------
Conventions inherited from L2-A, still load-bearing
--------------------------------------------------------------------------

Decorator order is ``@login_required`` → ``@require_*`` → ``@require_GET``/
``@require_POST``, and the method check being innermost matters: a cross-tenant
GET must answer 404 from the middleware rather than 405 from the method guard,
which is what the IDOR sweep's "at least one method answers 404" contract relies
on.

Mutations answer :func:`apps.common.htmx.toast_response` — a 204 carrying
``HX-Trigger`` — and the affected container re-fetches itself on the event.
Failures are **also 204**, deliberately: htmx does not process ``HX-Trigger`` on
a non-2xx response by default, so answering 400 would swallow the very message
the user needs to read.

--------------------------------------------------------------------------
What the IDOR sweep cannot see
--------------------------------------------------------------------------

``tests/idor.py`` walks URL kwargs, so every ``<uuid:…>`` below is covered
automatically once its resolver is registered. It does **not** walk the query
string, and this module puts three tenant-shaped things there: ``?segment=``,
``?filter=`` (which names tag, field and segment ids inside a JSON document) and
the bulk endpoints' ``ids``. All three are resolved through
``get_scoped_object_or_404`` or a ``.for_workspace()`` filter, and all three have
their own cross-tenant tests in ``tests/test_views.py``.
"""

from typing import Any
from uuid import UUID

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Q, QuerySet
from django.http import Http404, HttpResponse, StreamingHttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlencode
from django.views.decorators.http import require_GET, require_POST

from apps.common.htmx import toast_response
from apps.common.platforms import Platform
from apps.common.shortcuts import get_scoped_object_or_404
from apps.contacts import activity, conditions, export, imports, services
from apps.contacts.conditions import CONDITION_SCHEMA, ConditionError
from apps.contacts.errors import ContactsError
from apps.contacts.filters import (
    DEFAULT_SORT,
    SORTS,
    ContactQuery,
    contacts_for,
    parse_filter_document,
    resolve_query,
)
from apps.contacts.models import (
    Contact,
    ContactImport,
    ContactStatus,
    CustomField,
    CustomFieldType,
    CustomFieldValue,
    ImportDedupe,
    ImportStatus,
    Segment,
    Tag,
)
from apps.flows.engine import FlowNotRunnableError
from apps.members.decorators import require_permission, require_workspace_role
from apps.members.requests import WorkspaceRequest
from apps.members.roles import WorkspaceRole

PAGE_SIZE = 50

#: Cap on one bulk action. Selection is per page in the UI, so this is a bound
#: on a hand-crafted POST rather than on anything the page can produce — and it
#: is what stops "delete everyone" arriving as one request that runs for a
#: minute inside a web worker. Bulk work over a whole *segment* is a broadcast's
#: shape (issue #23), not a button on a list.
MAX_BULK_IDS = 500

#: Tags offered by the detail page's typeahead per keystroke.
TAG_SUGGESTIONS = 10


def _failed(exc: Exception, title: str) -> HttpResponse:
    return toast_response(tone="error", title=title, body=str(exc))


def _can(request: WorkspaceRequest, key: str) -> bool:
    return bool(request.workspace_membership.effective_permissions.get(key, False))


def _permissions(request: WorkspaceRequest) -> dict[str, Any]:
    """The two flags every CRM template branches on.

    Computed once per render and passed down rather than resolved in the
    template, because a template that asks the membership directly is a template
    that can disagree with the decorator on the view it is posting to — and the
    failure mode is a button that renders for someone who gets a 403 when they
    press it.
    """
    return {
        "can_edit_contacts": _can(request, "edit_contact_fields"),
        "can_manage_crm": _can(request, "manage_crm"),
    }


# ---------------------------------------------------------------------------
# The contact list
# ---------------------------------------------------------------------------


def _rows_context(request: WorkspaceRequest) -> dict[str, Any]:
    """What ``contacts/_rows.html`` draws: one page of contacts and its links.

    Read from ``request.GET`` and nowhere else. The mutating endpoints do not
    render the table at all — they answer a toast carrying ``contactsChanged``,
    and the container re-fetches itself with the querystring it already has. That
    is what keeps a bulk action from redrawing page one of an unfiltered list over
    whatever the operator was looking at.
    """
    query = resolve_query(request, request.workspace)
    rows, error = contacts_for(request.workspace, query)
    rows = activity.annotate_reachability(rows.prefetch_related("tags"), request.workspace)
    page = Paginator(rows, PAGE_SIZE).get_page(request.GET.get("page"))

    # Hung on the instances rather than passed as a {id: [...]} map, because a
    # Django template cannot subscript a dict by a variable key — the alternative
    # is a global ``dict_get`` filter, which is a lot of surface area for one
    # column. ``platforms_for`` is still one query for the whole page.
    found = activity.platforms_for(page.object_list, request.workspace)
    for contact in page.object_list:
        contact.channel_platforms = found.get(contact.pk, [])  # type: ignore[attr-defined]

    return {
        "page": page,
        "query": query,
        # For the select-this-page checkbox. Strings, because that is what the
        # per-row checkbox values are and the comparison in Alpine is ===.
        "page_ids": [str(contact.pk) for contact in page.object_list],
        "list_error": error or query.error,
        "sorts": _sort_options(),
        # Everything the pagination and export links have to carry forward,
        # already encoded. Rebuilding it in the template would mean the filter
        # JSON being urlencoded by hand in three places.
        "querystring": _querystring(query),
        **_permissions(request),
    }


def _sort_options() -> list[dict[str, str]]:
    """Labels for the sort control, keyed by the same names ``SORTS`` uses.

    A parallel list rather than labels on ``SORTS`` itself: that dict is the
    ORM's, and putting display strings in it would make a UI change a change to
    the module the ordering allowlist lives in.
    """
    labels = {
        "recent": "Last interaction",
        "oldest": "Least recent",
        "name": "Name (A–Z)",
        "name_desc": "Name (Z–A)",
        "email": "Email",
        "created": "Newest first",
    }
    return [{"value": key, "label": labels.get(key, key)} for key in SORTS]


def _querystring(query: ContactQuery) -> str:
    """``filter=…&q=…&sort=…`` for the links that must preserve the view.

    Built with ``urlencode`` rather than string concatenation because the filter
    is a JSON document full of quotes and braces, and ``segment`` is preferred
    over ``filter`` when both could apply — one canonical spelling per view, so a
    "next page" link and an "export" link cannot describe different sets.
    """
    params: dict[str, str] = {}
    if query.segment is not None:
        params["segment"] = str(query.segment.pk)
    elif query.document:
        params["filter"] = query.raw_filter
    if query.search_term:
        params["q"] = query.search_term
    if query.sort != DEFAULT_SORT:
        params["sort"] = query.sort
    return urlencode(params)


def _filter_config(workspace: Any, query: ContactQuery) -> dict[str, Any]:
    """Everything the filter builder needs to render §11.4, in one payload.

    ``CONDITION_SCHEMA["x-brightbean"]`` already carries the operator tables, the
    valueless-operator set, the operator labels, the system fields, the relative
    units, which sources this deployment cannot evaluate, and the limits — that
    extension block exists precisely so a consumer does not have to keep a second
    copy. So the builder reads it, and an operator added to
    :mod:`apps.contacts.conditions` shows up in this UI with no edit here.

    Only two things are added, because neither can live in a static schema: each
    source's label, evaluability and owning issue from the registry, and this
    workspace's own tags, fields and segments.

    One dict rather than six template variables, because it is one ``x-data``
    argument — and assembling it in the template would put the payload's shape
    somewhere Python cannot see it.
    """
    registry = conditions.sources()
    return {
        "sources": [
            {
                "name": name,
                "label": registry[name].label,
                "keyKind": registry[name].key_kind,
                "evaluable": registry[name].is_evaluable,
                # Carried so a greyed-out row can say *why* it is unavailable —
                # "arrives with issue #22" beats a control that does nothing.
                "owner": registry[name].owner,
            }
            for name in conditions.SOURCE_NAMES
        ],
        "vocabulary": CONDITION_SCHEMA["x-brightbean"],
        "platforms": [{"value": value, "label": label} for value, label in Platform.choices],
        "tags": [
            {"value": str(row.pk), "label": row.name} for row in Tag.objects.for_workspace(workspace).order_by("name")
        ],
        "fields": [
            {"value": str(row.pk), "label": row.name, "type": row.type}
            for row in CustomField.objects.for_workspace(workspace).order_by("name")
        ],
        "segments": [
            {"value": str(row.pk), "label": row.name}
            for row in Segment.objects.for_workspace(workspace).order_by("name")
        ],
        # The document the builder hydrates from. Taken from the parsed query
        # rather than re-read from the URL, so a segment loaded off disk
        # round-trips exactly as stored instead of through a re-serialisation
        # that could normalise it — which is the acceptance criterion this page
        # is judged on.
        "document": query.document,
        "segmentId": str(query.segment.pk) if query.segment is not None else "",
    }


@login_required
@require_workspace_role(WorkspaceRole.VIEWER)
@require_GET
def contact_list(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """The CRM's front page: filter bar, toolbar and the first page of contacts."""
    context = _rows_context(request)
    return render(
        request,
        "contacts/list.html",
        {
            **context,
            "filter_config": _filter_config(request.workspace, context["query"]),
            "segment_rows": Segment.objects.for_workspace(request.workspace).order_by("name"),
        },
    )


@login_required
@require_workspace_role(WorkspaceRole.VIEWER)
@require_GET
def contact_rows(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """Just the table, for every search keystroke, sort, page and filter change.

    Same reasoning as ``tag_rows``: without it a refresh renders the whole page
    — base template, sidebar, workspace switcher — so htmx can parse all of it
    and keep one div.

    ``HX-Push-Url`` rather than ``hx-push-url`` on the form. The form GETs *this*
    endpoint, so htmx would push the partial's address and hand the reader a
    bookmark to a bare ``<table>``. The server knows the page's own URL and the
    canonical spelling of the current view, so it says so here.
    """
    context = _rows_context(request)
    response = render(request, "contacts/_rows.html", context)
    page = request.GET.get("page") or ""
    parts = [part for part in (context["querystring"], f"page={page}" if page else "") if part]
    listing = reverse("contacts:list", kwargs={"workspace_id": request.workspace.pk})
    response["HX-Push-Url"] = f"{listing}?{'&'.join(parts)}" if parts else listing
    return response


# ---------------------------------------------------------------------------
# The contact detail page
# ---------------------------------------------------------------------------


def _contact_or_404(request: WorkspaceRequest, contact_id: Any) -> Contact:
    """Fetch a contact, refusing tombstones as firmly as another tenant's rows.

    A soft-deleted contact answers 404 rather than rendering: every other surface
    in the app starts from active contacts, and a detail page that still opened
    for one would be a way to keep editing somebody the operator believes they
    removed — and to re-add them to a segment by tagging them.
    """
    contact = get_scoped_object_or_404(Contact, request.workspace, pk=contact_id)
    if contact.status != ContactStatus.ACTIVE:
        raise Http404("No such contact.")
    return contact


def _field_values(contact: Contact) -> list[dict[str, Any]]:
    """Every custom field in the workspace, with this contact's value or none.

    Every field, not only the ones with a value: the page is an editor, and a
    field a contact has no value for is exactly the one somebody came here to
    fill in. ``CustomFieldType`` rides along so the template can pick a widget
    without a second lookup per row.
    """
    stored = {
        row.field_id: row
        for row in CustomFieldValue.objects.for_workspace(contact.workspace_id)
        .filter(contact=contact)
        .select_related("field")
    }
    rows: list[dict[str, Any]] = []
    for field in CustomField.objects.for_workspace(contact.workspace_id).order_by("name"):
        row = stored.get(field.pk)
        value = row.value if row is not None else None
        rows.append(
            {
                "field": field,
                "value": value,
                "has_value": row is not None,
                # Pre-rendered for the input's ``value=``. Django's ``date``
                # filter can produce these, but ``datetime-local`` wants
                # ``Y-m-d\TH:i`` — a format string whose backslash escape has to
                # survive a template literal — and a boolean needs the strings
                # "true"/"false" rather than Python's repr. Both are decisions
                # about *this* widget, so they are made here and the template
                # renders one variable.
                "form_value": _form_value(field, value),
                "is_boolean": field.type == CustomFieldType.BOOLEAN,
                "is_date": field.type == CustomFieldType.DATE,
                "is_datetime": field.type == CustomFieldType.DATETIME,
                "is_number": field.type == CustomFieldType.NUMBER,
            }
        )
    return rows


def _form_value(field: CustomField, value: Any) -> str:
    """One stored custom-field value, as the matching HTML input wants it."""
    if value is None:
        return ""
    if field.type == CustomFieldType.BOOLEAN:
        return "true" if value else "false"
    if field.type == CustomFieldType.DATE:
        return value.isoformat()
    if field.type == CustomFieldType.DATETIME:
        # Local time, minute precision: ``datetime-local`` refuses an offset, and
        # showing UTC to an operator in Berlin is a value they will "correct".
        return timezone.localtime(value).strftime("%Y-%m-%dT%H:%M")
    return str(value)


def _activity_context(request: WorkspaceRequest, contact: Contact) -> dict[str, Any]:
    """What ``contacts/_activity.html`` draws.

    Its own builder because the pane refreshes on its own: starting or stopping a
    flow changes the execution half and nothing else on the page, and rebuilding
    the identities and the field editor to redraw one status line would be three
    extra queries per click.
    """
    return {
        "contact": contact,
        # NOT "messages". That name is the Django messages framework's, supplied
        # by a context processor and rendered by base.html as flash alerts — so
        # binding it here put the repr of every MessagePreview in a banner across
        # the top of the page.
        "recent_messages": activity.recent_messages(contact),
        "execution": activity.live_execution(contact),
        "startable_flows": activity.startable_flows(request.workspace),
        **_permissions(request),
    }


@login_required
@require_workspace_role(WorkspaceRole.VIEWER)
@require_GET
def contact_detail(request: WorkspaceRequest, workspace_id: str, contact_id: str) -> HttpResponse:
    """One contact: header, editable fields, tags, channels and activity."""
    contact = _contact_or_404(request, contact_id)
    channels = activity.identities_for(contact)
    return render(
        request,
        "contacts/detail.html",
        {
            **_activity_context(request, contact),
            "field_values": _field_values(contact),
            "contact_tags": list(contact.tags.order_by("name")),
            "channels": channels,
            "avatar_url": activity.avatar_url(channels),
            # Name, label and current value per row. A Django template cannot
            # read an attribute whose name is in a variable, and the alternative
            # — a global ``attr`` filter — is a lot of surface area for one loop.
            "editable_fields": [
                {
                    "name": name,
                    "label": name.replace("_", " ").capitalize(),
                    "value": getattr(contact, name),
                }
                for name in services.EDITABLE_FIELDS
            ],
            "now": timezone.now(),
        },
    )


@login_required
@require_workspace_role(WorkspaceRole.VIEWER)
@require_GET
def contact_activity(request: WorkspaceRequest, workspace_id: str, contact_id: str) -> HttpResponse:
    """The activity pane alone, re-fetched after an automation change."""
    contact = _contact_or_404(request, contact_id)
    return render(request, "contacts/_activity.html", _activity_context(request, contact))


@login_required
@require_workspace_role(WorkspaceRole.VIEWER)
@require_GET
def contact_channels(request: WorkspaceRequest, workspace_id: str, contact_id: str) -> HttpResponse:
    """The channel-identities pane alone, re-fetched after an opt-out."""
    contact = _contact_or_404(request, contact_id)
    channels = activity.identities_for(contact)
    return render(
        request,
        "contacts/_channels.html",
        {"contact": contact, "channels": channels, "now": timezone.now(), **_permissions(request)},
    )


@login_required
@require_workspace_role(WorkspaceRole.VIEWER)
@require_GET
def contact_tags(request: WorkspaceRequest, workspace_id: str, contact_id: str) -> HttpResponse:
    """The tag chips alone, re-fetched after an add or a remove."""
    contact = _contact_or_404(request, contact_id)
    return render(
        request,
        "contacts/_tag_chips.html",
        {"contact": contact, "contact_tags": list(contact.tags.order_by("name")), **_permissions(request)},
    )


# ---------------------------------------------------------------------------
# Contact mutations
# ---------------------------------------------------------------------------


@login_required
@require_permission("edit_contact_fields")
@require_POST
def contact_edit(request: WorkspaceRequest, workspace_id: str, contact_id: str) -> HttpResponse:
    """Write the system fields the inline editor submitted.

    Only the names present in the POST are touched, so the single-field editors
    on the page can each post one input without the other five arriving empty and
    clearing themselves. ``update_contact`` holds the allowlist; this view does
    not filter, so a field added there appears here with no edit.
    """
    contact = _contact_or_404(request, contact_id)
    submitted = {name: request.POST[name] for name in services.EDITABLE_FIELDS if name in request.POST}
    if not submitted:
        return toast_response(tone="info", title="Nothing to save")
    try:
        changed = services.update_contact(contact, **submitted)
    except ContactsError as exc:
        return _failed(exc, "Could not save the contact")
    if not changed:
        return toast_response(tone="info", title="No change")
    return toast_response(
        tone="success",
        title="Contact saved",
        body=contact.display_name,
        events={"contactChanged": True},
    )


@login_required
@require_permission("edit_contact_fields")
@require_POST
def contact_field_value(request: WorkspaceRequest, workspace_id: str, contact_id: str, field_id: str) -> HttpResponse:
    """Set or clear one custom-field value.

    An empty submission **clears** the value rather than storing an empty string:
    ``clear_field_value`` deletes the row, which is what keeps the check
    constraint's "exactly one column populated" true rather than "at most one",
    and it is what the condition engine's ``no_value`` operator means.

    A checkbox that is unticked is absent from the POST entirely, which would
    read as "clear" — right for a text box and wrong for a boolean, where the
    absence *is* the value ``false``. Hence the explicit type branch.
    """
    contact = _contact_or_404(request, contact_id)
    field = get_scoped_object_or_404(CustomField, request.workspace, pk=field_id)
    raw = request.POST.get("value", "")

    try:
        if field.type == CustomFieldType.BOOLEAN:
            services.set_field_value(contact, field, raw in {"true", "on", "1"})
        elif raw == "":
            services.clear_field_value(contact, field)
        else:
            services.set_field_value(contact, field, raw)
    except ContactsError as exc:
        return _failed(exc, f"Could not save {field.name}")
    return toast_response(tone="success", title=f"{field.name} saved", events={"contactChanged": True})


@login_required
@require_permission("edit_contact_fields")
@require_POST
def contact_tag_add(request: WorkspaceRequest, workspace_id: str, contact_id: str) -> HttpResponse:
    """Attach a tag by id, or by name when the caller may also create tags.

    The split is the permission boundary made concrete. An Agent posts a
    ``tag_id`` chosen from the typeahead; only ``manage_crm`` may post a ``name``
    that does not exist yet, because minting a tag changes the workspace's
    vocabulary and every segment and flow that will later pick from it.
    """
    contact = _contact_or_404(request, contact_id)
    tag_id = request.POST.get("tag_id", "")
    name = request.POST.get("name", "").strip()

    if tag_id:
        tag = get_scoped_object_or_404(Tag, request.workspace, pk=tag_id)
    elif name:
        existing = Tag.objects.for_workspace(request.workspace).filter(name__iexact=name).first()
        if existing is None and not _can(request, "manage_crm"):
            return toast_response(
                tone="error",
                title="That tag does not exist",
                body="Creating tags needs the manage_crm permission. Ask an editor to add it first.",
            )
        try:
            tag, _created = services.get_or_create_tag(request.workspace, name)
        except ContactsError as exc:
            return _failed(exc, "Could not add the tag")
    else:
        return toast_response(tone="info", title="Pick a tag first")

    try:
        added = services.add_tag(contact, tag)
    except ContactsError as exc:
        return _failed(exc, "Could not add the tag")
    if not added:
        return toast_response(tone="info", title="Already tagged", body=tag.name)
    return toast_response(tone="success", title="Tag added", body=tag.name, events={"contactTagsChanged": True})


@login_required
@require_permission("edit_contact_fields")
@require_POST
def contact_tag_remove(request: WorkspaceRequest, workspace_id: str, contact_id: str, tag_id: str) -> HttpResponse:
    contact = _contact_or_404(request, contact_id)
    tag = get_scoped_object_or_404(Tag, request.workspace, pk=tag_id)
    removed = services.remove_tag(contact, tag)
    if not removed:
        return toast_response(tone="info", title="That tag was not on this contact")
    return toast_response(tone="success", title="Tag removed", body=tag.name, events={"contactTagsChanged": True})


@login_required
@require_permission("edit_contact_fields")
@require_GET
def tag_suggestions(request: WorkspaceRequest, workspace_id: str, contact_id: str) -> HttpResponse:
    """Tags matching the typeahead, minus the ones this contact already has."""
    contact = _contact_or_404(request, contact_id)
    term = request.GET.get("q", "").strip()[:100]
    rows = Tag.objects.for_workspace(request.workspace).exclude(contact_tags__contact=contact)
    if term:
        rows = rows.filter(name__icontains=term)
    matches = list(rows.order_by("name")[:TAG_SUGGESTIONS])
    return render(
        request,
        "contacts/_tag_suggestions.html",
        {
            "contact": contact,
            "suggestions": matches,
            "term": term,
            # Offer "create <term>" only when the term names nothing yet AND the
            # caller may mint one. Both halves matter: the second is the
            # permission, the first stops the control offering to create a tag
            # that is sitting right above it.
            "can_create": bool(term)
            and _can(request, "manage_crm")
            and not any(tag.name.casefold() == term.casefold() for tag in matches)
            and not Tag.objects.for_workspace(request.workspace).filter(name__iexact=term).exists(),
        },
    )


@login_required
@require_permission("edit_contact_fields")
@require_POST
def identity_opt_out(request: WorkspaceRequest, workspace_id: str, contact_id: str, identity_id: str) -> HttpResponse:
    """Record a manual opt-out on one channel identity (SPEC §19).

    Through ``activity.opt_out`` → the messaging facade → the single write site
    in ``messaging.ingest``, so the CRM never assigns ``opted_out_at`` itself and
    ROADMAP contract 3's one-writer property holds literally.

    There is **no un-opt-out**, here or anywhere. SPEC §19 puts opt-out at a
    chokepoint so it cannot be bypassed, and a toggle an operator could flip back
    is a bypass with a friendlier label; re-consent has to come from the contact.
    The template renders the control as a one-way action for that reason, not as
    a switch.
    """
    contact = _contact_or_404(request, contact_id)
    identity = activity.identity_for(contact, identity_id)
    if identity is None:
        raise Http404("No such identity.")

    changed = activity.opt_out(identity, source="manual")
    if not changed:
        return toast_response(tone="info", title="Already opted out")
    return toast_response(
        tone="success",
        title="Opted out",
        body=f"{identity.platform} · {identity.platform_user_id}",
        events={"contactChannelsChanged": True},
    )


@login_required
@require_permission("manage_crm")
@require_POST
def contact_start_flow(request: WorkspaceRequest, workspace_id: str, contact_id: str) -> HttpResponse:
    """Start a published flow for this contact, by hand (SPEC §5 ``started_by``).

    ``manage_crm`` rather than ``edit_flows``: the act is "do something to this
    contact", not "change this flow", and an Agent starting an automated
    conversation with somebody is a send decision an editor should own.
    """
    contact = _contact_or_404(request, contact_id)
    flow = activity.startable_flow(request.workspace, request.POST.get("flow_id", ""))
    if flow is None:
        return toast_response(
            tone="error",
            title="Could not start that flow",
            body="It has no published version, or it is archived.",
        )
    try:
        execution = activity.start_flow_for(contact, flow, actor=request.user)
    except (FlowNotRunnableError, ContactsError) as exc:
        return _failed(exc, "Could not start that flow")
    return toast_response(
        tone="success",
        title="Flow started",
        body=flow.name,
        events={"contactAutomationChanged": True, "executionId": str(execution.pk)},
    )


@login_required
@require_permission("manage_crm")
@require_POST
def contact_stop_automation(request: WorkspaceRequest, workspace_id: str, contact_id: str) -> HttpResponse:
    """Expire whatever this contact is running, through the engine.

    ``activity.stop_automation`` → ``apps.flows.engine.stop_automation``, which
    expires the run *and* cancels the queue rows that would have resumed it. A
    view writing ``execution.status`` would leave those armed, and the run the
    operator believed they had stopped would wake on its next timer.
    """
    contact = _contact_or_404(request, contact_id)
    stopped = activity.stop_automation(contact)
    if not stopped:
        return toast_response(tone="info", title="Nothing was running")
    return toast_response(
        tone="success",
        title="Automation stopped",
        body=f"{stopped} run{'' if stopped == 1 else 's'} expired.",
        events={"contactAutomationChanged": True},
    )


@login_required
@require_permission("manage_crm")
@require_POST
def contact_delete(request: WorkspaceRequest, workspace_id: str, contact_id: str) -> HttpResponse:
    """Soft-delete one contact, stopping its automation first.

    Order matters. ``delete_contact`` only sets ``status``; a live execution
    would carry on sending to a contact every surface has stopped showing, so
    automation is expired *before* the tombstone, while the contact is still
    something the engine will accept.
    """
    contact = _contact_or_404(request, contact_id)
    activity.stop_automation(contact)
    services.delete_contact(contact)
    return toast_response(
        tone="success",
        title="Contact deleted",
        body=contact.display_name,
        events={"contactsChanged": True},
    )


# ---------------------------------------------------------------------------
# Bulk actions
# ---------------------------------------------------------------------------
#
# Three endpoints rather than one with an `action` parameter, because the three
# gate differently: tagging is `edit_contact_fields`, deleting is `manage_crm`,
# and the sequence pair is L6-A's. One endpoint would mean the decorator taking
# the *lowest* bar and the view re-checking inside it — which is a permission
# check written twice, in a place where the second one is easy to forget.


def _selected(request: WorkspaceRequest) -> QuerySet[Contact]:
    """The contacts a bulk POST names. Scoped, deduplicated and capped.

    Ids the workspace does not own are simply absent from the result — the same
    "a miss, not a refusal" every other id in this app gets. The sweep in
    ``tests/idor.py`` cannot see these because they arrive in the body, so the
    cross-tenant case is tested directly.
    """
    ids = request.POST.getlist("ids")[:MAX_BULK_IDS]
    if not ids:
        return Contact.objects.for_workspace(request.workspace).none()
    valid: list[UUID] = []
    for raw in ids:
        try:
            valid.append(UUID(raw))
        except (ValueError, AttributeError, TypeError):
            continue
    return (
        Contact.objects.for_workspace(request.workspace)
        .filter(pk__in=valid, status=ContactStatus.ACTIVE)
        .order_by("id")
    )


def _bulk_result(title: str, body: str, events: dict[str, Any] | None = None) -> HttpResponse:
    return toast_response(tone="success", title=title, body=body, events=events or {"contactsChanged": True})


@login_required
@require_permission("edit_contact_fields")
@require_POST
def bulk_tag(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """Add or remove one tag across the selection.

    Row by row through ``services``, not a bulk insert, for the reason the whole
    app writes tags that way: ``contact.tag_added`` is what issue #22's rule
    triggers and #25's webhooks subscribe to, and a link row inserted behind the
    services layer is a change the rest of the product never learns about. At the
    500-row cap that is 500 short statements, which is a bounded cost paid
    knowingly.
    """
    tag = get_scoped_object_or_404(Tag, request.workspace, pk=request.POST.get("tag_id", ""))
    removing = request.POST.get("mode") == "remove"
    contacts = list(_selected(request))
    if not contacts:
        return toast_response(tone="info", title="Nothing selected")

    touched = 0
    for contact in contacts:
        try:
            changed = services.remove_tag(contact, tag) if removing else services.add_tag(contact, tag)
        except ContactsError as exc:
            return _failed(exc, "Could not update the tags")
        touched += int(changed)
    verb = "removed from" if removing else "added to"
    return _bulk_result(
        f"Tag {verb} {touched} contact{'' if touched == 1 else 's'}",
        f"{tag.name} — {len(contacts)} selected.",
    )


@login_required
@require_permission("manage_crm")
@require_POST
def bulk_delete(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """Soft-delete the selection, stopping each one's automation first.

    ``manage_crm``, so an Agent cannot reach it — SPEC §4 gives them
    ``edit_contact_fields`` and not this, and the issue's acceptance criteria say
    so in as many words. The confirm dialog lives in the template
    (``hx-confirm``); this view is the enforcement.
    """
    contacts = list(_selected(request))
    if not contacts:
        return toast_response(tone="info", title="Nothing selected")
    deleted = 0
    for contact in contacts:
        activity.stop_automation(contact)
        deleted += int(services.delete_contact(contact))
    return _bulk_result(
        f"{deleted} contact{'' if deleted == 1 else 's'} deleted",
        "They are hidden everywhere and excluded from every segment.",
    )


@login_required
@require_permission("manage_crm")
@require_POST
def bulk_sequence(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """Subscribe or unsubscribe the selection — **not yet implemented** (L6-A).

    The endpoint exists and answers politely rather than 404ing, which is what
    "no-op tolerant until L6-A" asks for: the control is rendered disabled, so
    only a hand-made POST arrives here, and when issue #22 lands it fills this
    body in and enables the button. Answering 404 today would mean the route,
    the template control and the tests all arriving in that PR instead.
    """
    return toast_response(
        tone="info",
        title="Sequences are not available yet",
        body="Subscribing contacts to a sequence arrives with issue #22 (L6-A).",
    )


# ---------------------------------------------------------------------------
# Segments
# ---------------------------------------------------------------------------


def _posted_filter(request: WorkspaceRequest) -> dict[str, Any]:
    """The filter document a segment form submitted.

    Parsed here and validated by ``services.create_segment`` /
    ``update_segment``, which call ``conditions.validate`` — so the caps, the
    unknown-key refusal and the cycle check are the engine's, applied once, at
    the boundary that stores the document.
    """
    return parse_filter_document(request.POST.get("filter", ""))


@login_required
@require_permission("manage_crm")
@require_POST
def segment_create(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """ "Save as segment" — store the filter bar's current document under a name."""
    try:
        segment = services.create_segment(
            request.workspace,
            name=request.POST.get("name", ""),
            filter_json=_posted_filter(request),
        )
    except (ContactsError, ConditionError) as exc:
        return _failed(exc, "Could not save the segment")
    return toast_response(
        tone="success",
        title="Segment saved",
        body=segment.name,
        events={"segmentsChanged": True, "segmentSaved": str(segment.pk)},
    )


@login_required
@require_permission("manage_crm")
@require_POST
def segment_update(request: WorkspaceRequest, workspace_id: str, segment_id: str) -> HttpResponse:
    """Rename a segment, replace its rules, or both.

    One endpoint for both because ``update_segment`` already takes both as
    optional: splitting them would be two routes, two tests and two IDOR
    registrations for one form.
    """
    segment = get_scoped_object_or_404(Segment, request.workspace, pk=segment_id)
    name = request.POST.get("name")
    document = _posted_filter(request) if "filter" in request.POST else None
    try:
        services.update_segment(segment, name=name, filter_json=document)
    except (ContactsError, ConditionError) as exc:
        return _failed(exc, "Could not update the segment")
    return toast_response(tone="success", title="Segment updated", body=segment.name, events={"segmentsChanged": True})


@login_required
@require_permission("manage_crm")
@require_POST
def segment_delete(request: WorkspaceRequest, workspace_id: str, segment_id: str) -> HttpResponse:
    """Delete a saved segment.

    The rows it described are untouched — a segment is a saved question, not a
    container — but a *flow* whose condition node references it by id will start
    failing validation, which is why the template's confirm text says so.
    """
    segment = get_scoped_object_or_404(Segment, request.workspace, pk=segment_id)
    name = segment.name
    segment.delete()
    return toast_response(tone="success", title="Segment deleted", body=name, events={"segmentsChanged": True})


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------


@login_required
@require_permission("manage_crm")
@require_GET
def contact_export(request: WorkspaceRequest, workspace_id: str) -> StreamingHttpResponse:
    """Stream the current filter's contacts as CSV.

    ``manage_crm`` rather than the read gate the list uses, and the difference is
    deliberate: reading a page of contacts on screen and walking away with every
    contact's name, email, phone and custom fields in one file are not the same
    act. Viewer is read-only, not read-and-take.

    The response streams, so a fifty-thousand-contact workspace costs bounded
    memory in the web process and the browser starts receiving before the query
    finishes. Every cell is neutralised against spreadsheet formula injection —
    see :mod:`apps.contacts.export`.
    """
    query = resolve_query(request, request.workspace)
    rows, error = contacts_for(request.workspace, query)
    if error:
        # Fail closed, the same way the list does: a filter that will not compile
        # must not quietly export the whole workspace.
        raise Http404(error)

    response = StreamingHttpResponse(
        export.stream_contacts(request.workspace, rows),
        content_type="text/csv; charset=utf-8",
    )
    response["Content-Disposition"] = f'attachment; filename="{export.export_filename(request.workspace)}"'
    # The file is a list of people. Nothing about it should sit in a shared cache.
    response["Cache-Control"] = "no-store"
    return response


# ---------------------------------------------------------------------------
# CSV import
# ---------------------------------------------------------------------------
#
# Four steps, each its own POST: upload, map, check, import. The wizard is a
# sequence of small requests rather than one long one for the reason the whole
# import is batched — a fifty-thousand-row file must never be work a web request
# is holding.


def _import_or_404(request: WorkspaceRequest, import_id: Any) -> ContactImport:
    return get_scoped_object_or_404(ContactImport, request.workspace, pk=import_id)


def _import_context(request: WorkspaceRequest, run: ContactImport) -> dict[str, Any]:
    """What the wizard shows for one run, whichever step it is on.

    The header and the preview are read from the stored file every time rather
    than cached on the row. The file does not change, so a copy has nothing to be
    right about that this is not — and once the retention sweep has dropped the
    file, a finished run still renders its report because that half lives in
    columns.
    """
    header: list[str] = []
    rows: list[list[str]] = []
    problem = ""
    if run.file:
        try:
            header = imports.read_header(run)
            rows = imports.preview(run)
        except (imports.UnusableImportError, OSError, ValueError) as exc:
            problem = str(exc) or "That file could not be read."
    return {
        "run": run,
        # One row per column, carrying its own samples and its current target.
        # Assembled here because a Django template can neither index a list by a
        # loop variable nor subscript a dict by one, and the alternatives are two
        # global filters for one table.
        "columns": _mapping_rows(run, header, rows),
        "header": header,
        "preview_rows": rows,
        "file_problem": problem,
        "system_targets": [
            {"value": f"system:{name}", "label": name.replace("_", " ").capitalize()}
            for name in imports.IMPORTABLE_SYSTEM_FIELDS
        ],
        "field_targets": [
            {"value": f"field:{row.pk}", "label": row.name, "type": row.type}
            for row in CustomField.objects.for_workspace(request.workspace).order_by("name")
        ],
        "tags_target": imports.TAGS_TARGET,
        "dedupe_choices": ImportDedupe.choices,
        "max_rows": settings.CONTACT_IMPORT_MAX_ROWS,
        "max_mb": settings.CONTACT_IMPORT_MAX_BYTES // (1024 * 1024),
    }


def _mapping_rows(run: ContactImport, header: list[str], preview: list[list[str]]) -> list[dict[str, Any]]:
    """The mapping table's rows: heading, a few sample cells, current target.

    The mapping is keyed by column **index** (as a string, because JSON object
    keys are strings), so this is where the index becomes a name for the reader
    and stays an index for the document.
    """
    mapping = run.mapping if isinstance(run.mapping, dict) else {}
    return [
        {
            "index": index,
            "name": name,
            "samples": [row[index] for row in preview[:3] if index < len(row)],
            "target": mapping.get(str(index), ""),
        }
        for index, name in enumerate(header)
    ]


@login_required
@require_permission("manage_crm")
@require_GET
def import_list(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """The import landing page: the upload form and this workspace's past runs."""
    return render(
        request,
        "contacts/import_list.html",
        {
            "runs": ContactImport.objects.for_workspace(request.workspace).select_related("created_by")[:25],
            "max_rows": settings.CONTACT_IMPORT_MAX_ROWS,
            "max_mb": settings.CONTACT_IMPORT_MAX_BYTES // (1024 * 1024),
        },
    )


@login_required
@require_permission("manage_crm")
@require_POST
def import_upload(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """Step 1 — take the file and the consent acknowledgement.

    The consent box is not decoration and not a legal fig leaf: it states the one
    thing about this feature that surprises people, which is that importing a
    phone number does **not** make the contact reachable on WhatsApp or SMS. No
    identity is fabricated anywhere in this path (see
    :mod:`apps.contacts.imports`), so a workspace cannot use the importer to
    manufacture a consent record it does not have.

    The size check happens before the file is stored, and it reads
    ``UploadedFile.size`` — Django has already streamed the body to a temporary
    file by then, bounded by ``DATA_UPLOAD_MAX_MEMORY_SIZE`` and the proxy's own
    body cap, so this is the last of three gates rather than the only one.
    """
    upload = request.FILES.get("file")
    if upload is None:
        return toast_response(tone="error", title="Pick a CSV file first")
    if not request.POST.get("consent_ack"):
        return toast_response(
            tone="error",
            title="Confirm the note about reachability",
            body="Imported contacts cannot be messaged until they write to you first.",
        )
    if upload.size > settings.CONTACT_IMPORT_MAX_BYTES:
        megabytes = settings.CONTACT_IMPORT_MAX_BYTES // (1024 * 1024)
        return toast_response(tone="error", title="That file is too large", body=f"The limit is {megabytes} MB.")

    run = ContactImport(
        workspace=request.workspace,
        # The uploaded name is attacker-controlled text. It is stored to show the
        # operator which file this was, escaped at render, and it decides nothing:
        # `import_upload_to` builds the storage path from the workspace and the
        # row's own id.
        original_filename=(upload.name or "")[:255],
        consent_ack=True,
        created_by=request.user,
    )
    # Save the row first so the id exists for the storage path, then attach the
    # file: `upload_to` reads `instance.pk`, which is None until the insert.
    run.save()
    run.file.save(f"{run.pk}.csv", upload, save=True)

    try:
        imports.read_header(run)
    except (imports.UnusableImportError, OSError, ValueError) as exc:
        # The file first, then the row. A FileField has not deleted its own file
        # since Django 1.3, so dropping the row on its own would leave an
        # unreferenced spreadsheet of personal data in storage that no sweep can
        # find again.
        run.file.delete(save=False)
        run.delete()
        return _failed(exc, "That file could not be read")
    return toast_response(
        tone="success",
        title="File uploaded",
        body="Map its columns to finish.",
        events={"importCreated": str(run.pk)},
    )


@login_required
@require_permission("manage_crm")
@require_GET
def import_detail(request: WorkspaceRequest, workspace_id: str, import_id: str) -> HttpResponse:
    """The wizard for one run: mapping, preview, progress and report.

    One template for every step, branching on ``run.status``, because the steps
    share almost all of their chrome and a four-template wizard drifts apart at
    the edges. It also means the progress view and the report are the same URL,
    so a link an operator kept still works after the import finishes.
    """
    run = _import_or_404(request, import_id)
    return render(request, "contacts/import_detail.html", _import_context(request, run))


@login_required
@require_permission("manage_crm")
@require_GET
def import_progress(request: WorkspaceRequest, workspace_id: str, import_id: str) -> HttpResponse:
    """The progress and report panel alone, polled while a run is working."""
    run = _import_or_404(request, import_id)
    return render(request, "contacts/_import_progress.html", {"run": run})


@login_required
@require_permission("manage_crm")
@require_POST
def import_mapping(request: WorkspaceRequest, workspace_id: str, import_id: str) -> HttpResponse:
    """Step 2 — store the column mapping and the dedupe choice, then dry-run.

    Mapping keys are **column indexes**: a CSV may legitimately repeat a heading,
    and a name-keyed mapping collapses duplicates into whichever it saw last,
    importing data from a column nobody chose.

    Saving and checking are one action because they are one decision. A separate
    "now check it" button would let a run sit mapped-but-unchecked, which is a
    state whose only meaningful next step is the thing the button does.
    """
    run = _import_or_404(request, import_id)
    if run.is_running:
        return toast_response(tone="info", title="That import is already running")

    try:
        header = imports.read_header(run)
    except (imports.UnusableImportError, OSError, ValueError) as exc:
        return _failed(exc, "That file could not be read")
    mapping = {
        str(index): request.POST.get(f"column-{index}", "")
        for index in range(len(header))
        if request.POST.get(f"column-{index}", "")
    }
    dedupe = request.POST.get("dedupe", "")
    if dedupe not in ImportDedupe.values:
        dedupe = ImportDedupe.UPDATE

    try:
        imports.resolve_mapping(request.workspace, mapping, header)
    except ContactsError as exc:
        return _failed(exc, "That mapping cannot be used")

    run.mapping = mapping
    run.dedupe = dedupe
    run.next_offset = 0
    run.save(update_fields=["mapping", "dedupe", "next_offset", "updated_at"])
    imports.enqueue(run, mode=imports.MODE_DRY_RUN)
    return toast_response(
        tone="success",
        title="Checking the file",
        body="Nothing is written until you confirm.",
        events={"importChanged": True},
    )


@login_required
@require_permission("manage_crm")
@require_POST
def import_run(request: WorkspaceRequest, workspace_id: str, import_id: str) -> HttpResponse:
    """Step 4 — confirm the checked file and start writing.

    Refused unless the dry run has finished. That is the whole point of the dry
    run: an operator who has not seen the row errors has not been told what this
    is about to do, and the first thing they would learn is that it already did
    it.
    """
    run = _import_or_404(request, import_id)
    if run.status != ImportStatus.VALIDATED:
        return toast_response(
            tone="error",
            title="Check the file first",
            body="The preview has to finish before anything is imported.",
        )
    run.next_offset = 0
    run.save(update_fields=["next_offset", "updated_at"])
    imports.enqueue(run, mode=imports.MODE_IMPORT)
    return toast_response(
        tone="success", title="Import started", body="You can leave this page.", events={"importChanged": True}
    )


@login_required
@require_permission("manage_crm")
@require_GET
def import_report(request: WorkspaceRequest, workspace_id: str, import_id: str) -> StreamingHttpResponse:
    """Download the row errors as CSV.

    Streamed and cell-escaped exactly like the contact export: a row error quotes
    the value that caused it, so the report is a file full of attacker-controlled
    text heading for a spreadsheet.
    """
    run = _import_or_404(request, import_id)
    response = StreamingHttpResponse(_report_rows(run), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="import-{run.pk}-errors.csv"'
    response["Cache-Control"] = "no-store"
    return response


def _report_rows(run: ContactImport) -> Any:
    """The report's rows, header first. Escaping is ``export.csv_stream``'s."""

    def records() -> Any:
        yield ["row", "column", "problem"]
        for error in run.errors:
            if not isinstance(error, dict):  # pragma: no cover - defensive
                continue
            yield [error.get(key, "") for key in ("row", "column", "message")]
        if run.errors_truncated:
            hidden = run.error_count - len(run.errors)
            yield ["", "", f"{hidden} further row error(s) were not stored."]

    return export.csv_stream(records())


def _tag_context(request: WorkspaceRequest) -> dict[str, Any]:
    """Tags with the number of **live** contacts carrying each.

    The filter is the point: the M2M join reaches ContactTag, which keeps its
    rows when a contact is soft-deleted, so an unfiltered Count reports people
    the rest of the app has already stopped showing — and feeds that number into
    a prompt telling the operator how many contacts a delete will touch.
    """
    tags = Tag.objects.for_workspace(request.workspace).annotate(
        contact_count=Count("contacts", filter=Q(contacts__status=ContactStatus.ACTIVE))
    )
    return {"tags": tags}


def _field_context(request: WorkspaceRequest) -> dict[str, Any]:
    """Custom fields with the number of values held by live contacts."""
    fields = CustomField.objects.for_workspace(request.workspace).annotate(
        value_count=Count("values", filter=Q(values__contact__status=ContactStatus.ACTIVE))
    )
    return {"fields": fields, "field_types": CustomFieldType.choices}


# ---------------------------------------------------------------------------
# Settings: tags
# ---------------------------------------------------------------------------
#
# L2-A's, unchanged apart from the merge endpoint below. There is exactly one
# tag editor in this app: the contact detail page attaches and detaches tags,
# and this page is where they are created, renamed, merged and deleted.


@login_required
@require_permission("manage_crm")
@require_GET
def tag_list(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    return render(request, "contacts/tag_list.html", _tag_context(request))


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


@login_required
@require_permission("manage_crm")
@require_POST
def tag_merge(request: WorkspaceRequest, workspace_id: str, tag_id: str) -> HttpResponse:
    """Fold this tag into another one and delete it.

    Two tags meaning the same thing is the data-quality problem the
    case-insensitive unique index cannot catch — "VIP" and "Priority" are
    distinct strings and one idea. ``services.merge_tags`` moves the links in a
    single transaction, so the workspace is never half-migrated.
    """
    source = get_scoped_object_or_404(Tag, request.workspace, pk=tag_id)
    target = get_scoped_object_or_404(Tag, request.workspace, pk=request.POST.get("target_id", ""))
    try:
        moved = services.merge_tags(source, target)
    except ContactsError as exc:
        return _failed(exc, "Could not merge the tags")
    return toast_response(
        tone="success",
        title="Tags merged",
        body=f"{moved} contact{'' if moved == 1 else 's'} moved to {target.name}.",
        events={"tagsChanged": True},
    )


# ---------------------------------------------------------------------------
# Settings: custom fields
# ---------------------------------------------------------------------------
#
# Also L2-A's. Retyping a field — with a preview of the values it would discard
# — is deliberately still absent: ``services.rename_custom_field`` explains why
# a silent retype orphans every stored value, and the issue body scopes this page
# to "CRUD with type; deletion warns about values", which is what it does.


@login_required
@require_permission("manage_crm")
@require_GET
def field_list(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    return render(request, "contacts/field_list.html", _field_context(request))


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


@login_required
@require_permission("manage_crm")
@require_GET
def tag_rows(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """Just the table, for the htmx refresh after a mutation.

    Without it the refresh renders the whole page — the base template, the
    sidebar, the workspace switcher — so htmx can parse all of it and keep one
    div. The response goes from tens of kilobytes to a few hundred bytes.

    It does **not** save the shell's queries: ``sidebar_context`` is a context
    processor and runs for every ``render()``, whatever the template. Cutting
    those would mean bypassing ``RequestContext`` entirely, which is a bigger
    change than this is worth.
    """
    return render(request, "contacts/_tag_rows.html", _tag_context(request))


@login_required
@require_permission("manage_crm")
@require_GET
def field_rows(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """Just the table — see :func:`tag_rows`."""
    return render(request, "contacts/_field_rows.html", _field_context(request))
