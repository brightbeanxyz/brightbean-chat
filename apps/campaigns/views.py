"""The sequence list, the step editor and the subscriber panel.

Reads are open to any workspace member and writes require ``edit_flows`` — the
same split ``apps/flows/views.py`` uses, and the same permission, because a
sequence is a schedule over flows and anyone who may edit one may edit the
other. ``PERMISSION_KEYS`` in ``apps/members/roles.py`` is the whole vocabulary;
this app invents none.

Everything except the two pages is HTMX: mutations answer with a toast and a
``sequencesChanged`` or ``sequenceStepsChanged`` event, and the affected partial
re-fetches itself. One renderer per region rather than one per action.

Every mutation answers **2xx even when it refuses**, following
``apps/flows/views_triggers.py``: htmx drops ``HX-Trigger`` on a non-2xx
response, so a 400 would show the user no toast at all and the request would
simply appear to do nothing.
"""

from typing import Any

from django.contrib.auth.decorators import login_required
from django.db.models import QuerySet
from django.http import HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from apps.campaigns import selectors, services
from apps.campaigns.errors import CampaignsError
from apps.campaigns.models import (
    DelayUnit,
    EnrollmentStatus,
    Sequence,
    SequenceEnrollment,
    SequenceStatus,
    SequenceStep,
)
from apps.common.htmx import toast_response
from apps.common.shortcuts import get_scoped_object_or_404
from apps.common.windows import WEEKDAYS
from apps.contacts.filters import MAX_SEARCH_CHARS, search
from apps.contacts.models import Contact, ContactStatus
from apps.flows.models import Flow, FlowStatus
from apps.members.decorators import require_permission, require_workspace_role
from apps.members.requests import WorkspaceRequest
from apps.members.roles import WorkspaceRole

__all__ = [
    "sequence_create",
    "sequence_delete",
    "sequence_detail",
    "sequence_list",
    "sequence_rename",
    "sequence_status",
    "step_create",
    "step_delete",
    "step_move",
    "step_update",
    "steps_panel",
    "subscriber_add",
    "subscriber_suggest",
    "subscriber_remove",
    "subscribers_panel",
]

# Viewer is the floor of the role ladder, so this is "any member".
require_workspace_member = require_workspace_role(WorkspaceRole.VIEWER)

#: How many contacts the subscriber typeahead offers. Short enough to read at a
#: glance; the CRM's bulk action is the answer to "I want fifty of them".
SUGGESTIONS = 8


def _sequence(request: WorkspaceRequest, sequence_id: str) -> Sequence:
    return get_scoped_object_or_404(Sequence, request.workspace, pk=sequence_id)


def _can_edit(request: WorkspaceRequest) -> bool:
    return bool(request.workspace_membership.effective_permissions.get("edit_flows", False))


def _refused(exc: Exception, title: str) -> HttpResponse:
    return toast_response(tone="error", title=title, body=str(exc))


# ---------------------------------------------------------------------------
# Sequences
# ---------------------------------------------------------------------------


@login_required
@require_workspace_member
@require_GET
def sequence_list(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """The list. Answers the rows partial to HTMX and the page otherwise."""
    query = (request.GET.get("q") or "").strip()[:MAX_SEARCH_CHARS]
    # Sanitised once, before the query. An unrecognised value falls back to the
    # unfiltered view rather than to a filter matching nothing — the same
    # correction `apps/flows/views.py::_visible_flows` documents: with the
    # validation applied only to the context, `?status=bogus` returned an empty
    # list under the "create your first one" empty state, with no visible filter
    # to clear.
    status = (request.GET.get("status") or "").strip()
    if status not in SequenceStatus.values:
        status = ""
    context = {
        "sequences": list(selectors.sequences_for(request.workspace, query=query, status=status)),
        "status_options": list(SequenceStatus.choices),
        "query": query,
        "status": status,
        "can_edit": _can_edit(request),
    }
    template = "campaigns/_list_rows.html" if request.headers.get("HX-Request") else "campaigns/list.html"
    return render(request, template, context)


@login_required
@require_permission("edit_flows")
@require_POST
def sequence_create(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    try:
        sequence = services.create_sequence(request.workspace, name=request.POST.get("name", ""))
    except CampaignsError as exc:
        return _refused(exc, "Could not create the sequence")
    return toast_response(
        tone="success",
        title="Sequence created",
        body=f"{sequence.name} is ready for its first step.",
        events={"sequencesChanged": True},
    )


@login_required
@require_permission("edit_flows")
@require_POST
def sequence_rename(request: WorkspaceRequest, workspace_id: str, sequence_id: str) -> HttpResponse:
    sequence = _sequence(request, sequence_id)
    try:
        services.rename_sequence(sequence, name=request.POST.get("name", ""))
    except CampaignsError as exc:
        return _refused(exc, "Could not rename the sequence")
    return toast_response(tone="success", title="Sequence renamed", events={"sequencesChanged": True})


@login_required
@require_permission("edit_flows")
@require_POST
def sequence_status(request: WorkspaceRequest, workspace_id: str, sequence_id: str) -> HttpResponse:
    sequence = _sequence(request, sequence_id)
    try:
        services.set_status(sequence, status=(request.POST.get("status") or "").strip())
    except CampaignsError as exc:
        return _refused(exc, "Could not change the status")
    return toast_response(
        tone="success",
        title=f"Sequence {sequence.get_status_display().lower()}",
        body="Only active sequences accept new subscribers. People already on it are unaffected.",
        events={"sequencesChanged": True},
    )


@login_required
@require_permission("edit_flows")
@require_POST
def sequence_delete(request: WorkspaceRequest, workspace_id: str, sequence_id: str) -> HttpResponse:
    sequence = _sequence(request, sequence_id)
    name = sequence.name
    services.delete_sequence(sequence)
    return toast_response(
        tone="success",
        title="Sequence deleted",
        body=f"{name} and everyone's progress through it.",
        events={"sequencesChanged": True},
    )


@login_required
@require_workspace_member
@require_GET
def sequence_detail(request: WorkspaceRequest, workspace_id: str, sequence_id: str) -> HttpResponse:
    """The editor: ordered steps, per-step counts and the subscriber panel."""
    sequence = _sequence(request, sequence_id)
    return render(request, "campaigns/detail.html", _detail_context(request, sequence))


def _detail_context(request: WorkspaceRequest, sequence: Sequence) -> dict[str, Any]:
    can_edit = _can_edit(request)
    return {
        "sequence": sequence,
        "steps": selectors.steps_for(sequence),
        "delay_units": list(DelayUnit.choices),
        # (value, label) pairs for the window's weekday checkboxes, off the
        # shared table rather than a second list of day names in a template.
        "weekdays": [(day, day.title()) for day in WEEKDAYS],
        "status_options": list(SequenceStatus.choices),
        "can_edit": can_edit,
        # Behind `can_edit` because `campaigns/_step_fields.html` — its only
        # consumer — is included solely inside `{% if can_edit %}` blocks. A
        # Viewer refreshing the steps panel was paying a scan of every active
        # flow in the workspace for a picker never rendered.
        "flow_options": _flow_options(request) if can_edit else [],
    }


def _flow_options(request: WorkspaceRequest) -> list[Any]:
    """Published flows a step may start.

    Only active ones: a step pointing at a draft is a step whose every run logs
    "cannot start", so the picker should not be able to build one. The "New flow"
    shortcut beside it is the answer to "the flow I want does not exist yet".
    """
    return list(
        Flow.objects.for_workspace(request.workspace)
        .filter(status=FlowStatus.ACTIVE)
        .order_by("name")
        .values("id", "name")
    )


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


@login_required
@require_workspace_member
@require_GET
def steps_panel(request: WorkspaceRequest, workspace_id: str, sequence_id: str) -> HttpResponse:
    """The steps region on its own. Re-fetched by every step mutation."""
    sequence = _sequence(request, sequence_id)
    return render(request, "campaigns/_steps.html", _detail_context(request, sequence))


def _step(request: WorkspaceRequest, sequence: Sequence, step_id: str) -> SequenceStep:
    return get_scoped_object_or_404(SequenceStep, request.workspace, pk=step_id, sequence=sequence)


def _flow(request: WorkspaceRequest, raw_id: Any) -> Flow:
    """The posted flow, scoped. A missing or foreign id is a 404, not a 403."""
    return get_scoped_object_or_404(Flow, request.workspace, pk=raw_id or "")


def _window_from(request: WorkspaceRequest) -> dict[str, Any]:
    """The send window a step form posted.

    Normalised by ``services._clean_window``, which is the allowlist; this only
    has to produce something shaped like one.
    """
    return {
        "enabled": "window_enabled" in request.POST,
        "days": request.POST.getlist("window_days"),
        "from": request.POST.get("window_from", ""),
        "to": request.POST.get("window_to", ""),
        "use_contact_timezone": "window_contact_tz" in request.POST,
    }


@login_required
@require_permission("edit_flows")
@require_POST
def step_create(request: WorkspaceRequest, workspace_id: str, sequence_id: str) -> HttpResponse:
    sequence = _sequence(request, sequence_id)
    try:
        services.add_step(
            sequence,
            flow=_flow(request, request.POST.get("flow_id")),
            delay_value=request.POST.get("delay_value", 1),
            delay_unit=(request.POST.get("delay_unit") or DelayUnit.DAYS).strip(),
            send_window=_window_from(request),
        )
    except CampaignsError as exc:
        return _refused(exc, "Could not add the step")
    return toast_response(tone="success", title="Step added", events={"sequenceStepsChanged": True})


@login_required
@require_permission("edit_flows")
@require_POST
def step_update(request: WorkspaceRequest, workspace_id: str, sequence_id: str, step_id: str) -> HttpResponse:
    sequence = _sequence(request, sequence_id)
    step = _step(request, sequence, step_id)
    try:
        services.update_step(
            step,
            flow=_flow(request, request.POST.get("flow_id")),
            delay_value=request.POST.get("delay_value", step.delay_value),
            delay_unit=(request.POST.get("delay_unit") or step.delay_unit).strip(),
            send_window=_window_from(request),
        )
    except CampaignsError as exc:
        return _refused(exc, "Could not save the step")
    return toast_response(tone="success", title="Step saved", events={"sequenceStepsChanged": True})


@login_required
@require_permission("edit_flows")
@require_POST
def step_move(request: WorkspaceRequest, workspace_id: str, sequence_id: str, step_id: str) -> HttpResponse:
    sequence = _sequence(request, sequence_id)
    step = _step(request, sequence, step_id)
    try:
        services.move_step(step, direction=(request.POST.get("direction") or "").strip())
    except CampaignsError as exc:
        return _refused(exc, "Could not move the step")
    return toast_response(tone="success", title="Order updated", events={"sequenceStepsChanged": True})


@login_required
@require_permission("edit_flows")
@require_POST
def step_delete(request: WorkspaceRequest, workspace_id: str, sequence_id: str, step_id: str) -> HttpResponse:
    sequence = _sequence(request, sequence_id)
    services.delete_step(_step(request, sequence, step_id))
    return toast_response(
        tone="success",
        title="Step removed",
        body="Anyone waiting on it moves to whatever took its place.",
        events={"sequenceStepsChanged": True},
    )


# ---------------------------------------------------------------------------
# Subscribers
# ---------------------------------------------------------------------------


@login_required
@require_workspace_member
@require_GET
def subscribers_panel(request: WorkspaceRequest, workspace_id: str, sequence_id: str) -> HttpResponse:
    sequence = _sequence(request, sequence_id)
    status = (request.GET.get("status") or EnrollmentStatus.ACTIVE).strip()
    if status not in EnrollmentStatus.values:
        status = EnrollmentStatus.ACTIVE
    page = selectors.subscribers_for(sequence, status=status)
    return render(
        request,
        "campaigns/_subscribers.html",
        {
            "sequence": sequence,
            "enrollments": page.rows,
            "subscriber_total": page.total,
            "subscriber_cap": selectors.MAX_SUBSCRIBERS,
            "truncated": page.truncated,
            "status": status,
            "status_options": list(EnrollmentStatus.choices),
            "can_edit": _can_edit(request),
        },
    )


@login_required
@require_permission("edit_flows")
@require_GET
def subscriber_suggest(request: WorkspaceRequest, workspace_id: str, sequence_id: str) -> HttpResponse:
    """Contacts matching the panel's typeahead, minus the ones already on it.

    The same shape ``contacts:tag_suggest`` uses: a plain htmx GET into a
    results list, each row POSTing the id it names. Asking an operator to paste
    a UUID would be the alternative, and "manual enrollment from the contact
    view" (SPEC §12) is not that.

    Gated on ``edit_flows`` rather than on membership: it is the input half of a
    control only an editor can use, and a reader who cannot subscribe anybody has
    no reason to be handed a contact search.
    """
    sequence = _sequence(request, sequence_id)
    term = (request.GET.get("q") or "").strip()[:MAX_SEARCH_CHARS]
    rows: QuerySet[Contact] = (
        Contact.objects.for_workspace(request.workspace)
        .filter(status=ContactStatus.ACTIVE)
        .exclude(sequence_enrollments__sequence=sequence, sequence_enrollments__status=EnrollmentStatus.ACTIVE)
    )
    if term:
        rows = search(rows, term)
    return render(
        request,
        "campaigns/_subscriber_suggestions.html",
        {
            "sequence": sequence,
            "suggestions": list(rows.order_by("first_name", "last_name", "-id")[:SUGGESTIONS]),
            "term": term,
        },
    )


@login_required
@require_permission("edit_flows")
@require_POST
def subscriber_add(request: WorkspaceRequest, workspace_id: str, sequence_id: str) -> HttpResponse:
    """Manual enrollment from the sequence page.

    The contact is named by id in the body rather than in the URL, so the IDOR
    sweep cannot reach it — which is why the lookup is scoped here and covered
    by a direct cross-tenant test, the same arrangement
    ``apps/contacts/views.py::_selected`` documents.
    """
    sequence = _sequence(request, sequence_id)
    contact = get_scoped_object_or_404(
        Contact, request.workspace, pk=request.POST.get("contact_id") or "", status=ContactStatus.ACTIVE
    )
    try:
        services.subscribe(sequence, contact, source="manual")
    except CampaignsError as exc:
        return _refused(exc, "Could not subscribe that contact")
    return toast_response(
        tone="success",
        title="Contact subscribed",
        body=f"{contact.display_name} starts at step 1.",
        events={"sequenceSubscribersChanged": True, "sequenceStepsChanged": True},
    )


@login_required
@require_permission("edit_flows")
@require_POST
def subscriber_remove(
    request: WorkspaceRequest, workspace_id: str, sequence_id: str, enrollment_id: str
) -> HttpResponse:
    sequence = _sequence(request, sequence_id)
    enrollment = get_scoped_object_or_404(SequenceEnrollment, request.workspace, pk=enrollment_id, sequence=sequence)
    services.unsubscribe(sequence, enrollment.contact)
    return toast_response(
        tone="success",
        title="Contact unsubscribed",
        body="Future steps are cancelled. Anything already running finishes.",
        events={"sequenceSubscribersChanged": True, "sequenceStepsChanged": True},
    )
