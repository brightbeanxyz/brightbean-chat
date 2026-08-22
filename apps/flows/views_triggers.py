"""The Triggers panel — HTMX CRUD on the flow page, plus the ref-link QR codes.

A module of its own rather than more of ``apps/flows/views.py``, following
``apps/channels/views_webhooks.py``: that file has one concern and an explicit
``__all__``, and eight more views with a different concern would end both.

Reads are open to any workspace member and writes need ``edit_flows`` — the same
split the flow list and the builder API already use, and for the same reason. A
Viewer can open the panel and see what starts this flow; only an Editor changes it.

Every mutation answers **2xx even when it refuses**. htmx drops ``HX-Trigger`` on
a non-2xx response, so a 400 would show the user no toast at all — the request
would simply appear to do nothing. A refusal is an error toast *without* the
``triggersChanged`` event, which leaves the drawer exactly as they left it.
"""

import logging
from typing import Any

from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from apps.common.htmx import toast_response
from apps.common.shortcuts import get_scoped_object_or_404
from apps.flows.models import Flow, Trigger, TriggerType
from apps.flows.triggers import forms, links, qr, services
from apps.flows.triggers.registry import TRIGGER_TYPES, spec_for
from apps.members.decorators import require_permission, require_workspace_role
from apps.members.requests import WorkspaceRequest
from apps.members.roles import WorkspaceRole

__all__ = [
    "trigger_create",
    "trigger_delete",
    "trigger_form",
    "trigger_move",
    "trigger_panel",
    "trigger_qr",
    "trigger_toggle",
    "trigger_update",
]

logger = logging.getLogger(__name__)

require_workspace_member = require_workspace_role(WorkspaceRole.VIEWER)

#: What ``?format=`` accepts on the QR endpoint. A query parameter rather than a
#: URL segment so the IDOR sweep needs no extra kwarg resolver, and so an
#: unrecognised value is a 400 about the format rather than a 404 that reads like
#: a tenancy failure.
_QR_FORMATS = {"svg": ("image/svg+xml", qr.render_svg), "png": ("image/png", qr.render_png)}

#: SPEC §10's rule events, with copy. Read off the schema so the form and the
#: validator cannot offer different lists.
_RULE_EVENTS: list[tuple[str, str]] = [
    ("tag_added", "A tag is added"),
    ("tag_removed", "A tag is removed"),
    ("field_changed", "A field changes"),
    ("sequence_subscribed", "Subscribed to a sequence"),
    ("sequence_unsubscribed", "Unsubscribed from a sequence"),
    ("contact_created", "A contact is created"),
]


@login_required
@require_workspace_member
@require_GET
def trigger_panel(request: WorkspaceRequest, workspace_id: str, flow_id: str) -> HttpResponse:
    """The drawer's body. Re-fetched by every mutation's ``triggersChanged``."""
    flow = get_scoped_object_or_404(Flow, request.workspace, pk=flow_id)
    return render(request, "flows/_triggers_panel.html", _panel_context(request, flow))


@login_required
@require_permission("edit_flows")
@require_GET
def trigger_form(request: WorkspaceRequest, workspace_id: str, flow_id: str) -> HttpResponse:
    """The create form for a chosen type, or the edit form for one trigger."""
    flow = get_scoped_object_or_404(Flow, request.workspace, pk=flow_id)
    raw_id = request.GET.get("trigger")
    trigger = get_scoped_object_or_404(Trigger, request.workspace, pk=raw_id, flow=flow) if raw_id else None
    trigger_type = trigger.type if trigger is not None else (request.GET.get("type") or TriggerType.KEYWORD)
    spec = spec_for(trigger_type)
    if spec is None:
        return toast_response(tone="error", title="Unknown trigger type", body="Pick one from the list.")

    context = _panel_context(request, flow)
    context.update(
        {
            "trigger": trigger,
            "spec": spec,
            "config": trigger.config_json if trigger is not None else spec.default_config(),
            "connection_options": _connection_options(request, spec),
            "rule_events": _RULE_EVENTS,
        }
    )
    return render(request, "flows/_trigger_form.html", context)


@login_required
@require_permission("edit_flows")
@require_POST
def trigger_create(request: WorkspaceRequest, workspace_id: str, flow_id: str) -> HttpResponse:
    flow = get_scoped_object_or_404(Flow, request.workspace, pk=flow_id)
    trigger_type = (request.POST.get("type") or "").strip()
    spec = spec_for(trigger_type)
    if spec is None:
        return toast_response(tone="error", title="Unknown trigger type", body="Pick one from the list.")

    try:
        config = forms.config_from_post(trigger_type, request.POST)
    except forms.KeywordMismatchError as exc:
        return toast_response(tone="error", title="Keywords did not save", body=str(exc))

    refused = _refuse_duplicate_ref(flow, trigger_type, config)
    if refused is not None:
        return refused

    try:
        services.create_trigger(
            flow,
            trigger_type=trigger_type,
            config=config,
            connection=_connection(request, spec),
        )
    except services.TriggerValidationError as exc:
        return _refusal(exc)
    return toast_response(tone="success", title="Trigger added", events={"triggersChanged": True})


@login_required
@require_permission("edit_flows")
@require_POST
def trigger_update(request: WorkspaceRequest, workspace_id: str, flow_id: str, trigger_id: str) -> HttpResponse:
    flow = get_scoped_object_or_404(Flow, request.workspace, pk=flow_id)
    trigger = get_scoped_object_or_404(Trigger, request.workspace, pk=trigger_id, flow=flow)
    spec = spec_for(trigger.type)
    if spec is None:  # pragma: no cover - a stored type with no spec
        return toast_response(tone="error", title="Unknown trigger type")

    try:
        config = forms.config_from_post(trigger.type, request.POST)
    except forms.KeywordMismatchError as exc:
        return toast_response(tone="error", title="Keywords did not save", body=str(exc))

    refused = _refuse_duplicate_ref(flow, trigger.type, config, exclude=trigger)
    if refused is not None:
        return refused

    try:
        services.update_trigger(
            trigger,
            config=config,
            connection=_connection(request, spec),
            connection_given=True,
        )
    except services.TriggerValidationError as exc:
        return _refusal(exc)
    return toast_response(tone="success", title="Trigger saved", events={"triggersChanged": True})


@login_required
@require_permission("edit_flows")
@require_POST
def trigger_toggle(request: WorkspaceRequest, workspace_id: str, flow_id: str, trigger_id: str) -> HttpResponse:
    flow = get_scoped_object_or_404(Flow, request.workspace, pk=flow_id)
    trigger = get_scoped_object_or_404(Trigger, request.workspace, pk=trigger_id, flow=flow)
    services.set_enabled(trigger, not trigger.enabled)
    return toast_response(
        tone="success",
        title="Trigger enabled" if trigger.enabled else "Trigger paused",
        events={"triggersChanged": True},
    )


@login_required
@require_permission("edit_flows")
@require_POST
def trigger_move(request: WorkspaceRequest, workspace_id: str, flow_id: str, trigger_id: str) -> HttpResponse:
    flow = get_scoped_object_or_404(Flow, request.workspace, pk=flow_id)
    trigger = get_scoped_object_or_404(Trigger, request.workspace, pk=trigger_id, flow=flow)
    try:
        services.move_trigger(trigger, direction=(request.POST.get("direction") or "").strip())
    except services.TriggerValidationError as exc:
        return _refusal(exc)
    return toast_response(tone="success", title="Order updated", events={"triggersChanged": True})


@login_required
@require_permission("edit_flows")
@require_POST
def trigger_delete(request: WorkspaceRequest, workspace_id: str, flow_id: str, trigger_id: str) -> HttpResponse:
    flow = get_scoped_object_or_404(Flow, request.workspace, pk=flow_id)
    trigger = get_scoped_object_or_404(Trigger, request.workspace, pk=trigger_id, flow=flow)
    services.delete_trigger(trigger)
    return toast_response(tone="success", title="Trigger removed", events={"triggersChanged": True})


@login_required
@require_workspace_member
@require_GET
def trigger_qr(
    request: WorkspaceRequest,
    workspace_id: str,
    flow_id: str,
    trigger_id: str,
    connection_id: str,
) -> HttpResponse:
    """A QR code for one connection's deep link (SPEC §21 phase 2).

    Addressed by connection because an unbound ref trigger covers Telegram *and*
    Messenger *and* Instagram, and those are three different accounts with three
    different links — one code per trigger would have to pick one and be wrong
    about the others.
    """
    flow = get_scoped_object_or_404(Flow, request.workspace, pk=flow_id)
    trigger = get_scoped_object_or_404(Trigger, request.workspace, pk=trigger_id, flow=flow, type=TriggerType.REF_URL)

    link = next((item for item in links.ref_links_for(trigger) if str(item.connection.pk) == str(connection_id)), None)
    if link is None or not link.available:
        # 404 rather than an explanation: a connection this trigger does not
        # cover, one whose handle is unknown, and one that does not exist should
        # all be indistinguishable from outside.
        raise Http404

    wanted = (request.GET.get("format") or "svg").lower()
    chosen = _QR_FORMATS.get(wanted)
    if chosen is None:
        return HttpResponse("Unsupported format.", status=400, content_type="text/plain")
    content_type, render_code = chosen

    response = HttpResponse(render_code(link.url), content_type=content_type)
    disposition = "attachment" if request.GET.get("download") else "inline"
    ref = (trigger.config_json or {}).get("ref") or "trigger"
    response["Content-Disposition"] = f'{disposition}; filename="{ref}-qr.{wanted}"'
    response["X-Content-Type-Options"] = "nosniff"
    # These bytes are generated here from a REF_PATTERN-validated ref and are
    # loaded through <img>, which cannot run script — but an SVG opened directly
    # is a document, so it is served inert (SECURITY-BASELINE §9).
    response["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; sandbox"
    response["Cache-Control"] = "private, max-age=3600"
    return response


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _panel_context(request: WorkspaceRequest, flow: Flow) -> dict[str, Any]:
    rows = list(services.triggers_for(flow))
    return {
        "flow": flow,
        "triggers": [_row(trigger) for trigger in rows],
        "trigger_types": [spec for spec in TRIGGER_TYPES.values() if not spec.entrypoint_only],
        "duplicate_stage_types": _duplicate_stage_types(rows),
        "can_edit": request.workspace_membership.effective_permissions.get("edit_flows", False),
    }


def _row(trigger: Trigger) -> dict[str, Any]:
    spec = spec_for(trigger.type)
    return {
        "trigger": trigger,
        "spec": spec,
        "summary": services.describe(trigger),
        "links": links.ref_links_for(trigger) if trigger.type == TriggerType.REF_URL else [],
    }


def _duplicate_stage_types(rows: list[Trigger]) -> list[str]:
    """Types where a second enabled trigger can never fire, so the panel can say so.

    ``default_reply`` and ``welcome`` are answered by whichever has the lower
    priority, every time — which is correct, uniform with every other type, and
    completely invisible unless somebody says it out loud.
    """
    seen: dict[str, int] = {}
    for trigger in rows:
        if trigger.enabled and trigger.type in {TriggerType.DEFAULT_REPLY, TriggerType.WELCOME}:
            seen[trigger.type] = seen.get(trigger.type, 0) + 1
    return sorted(trigger_type for trigger_type, count in seen.items() if count > 1)


def _connection_options(request: WorkspaceRequest, spec: Any) -> list[dict[str, str]]:
    """The connections this type can bind to. A convenience, never a gate."""
    from apps.flows.compat import installed_model

    if not spec.bindable:
        return []
    model = installed_model("channels", "apps.channels", "ChannelConnection")
    if model is None:  # pragma: no cover - channels is always installed
        return []
    rows = (
        model.objects.for_workspace(request.workspace)
        .filter(platform__in=sorted(spec.platforms))
        .order_by("platform", "display_name")
    )
    return [
        {"value": str(row.pk), "label": row.display_name, "platform": row.platform, "status": row.status}
        for row in rows
    ]


def _connection(request: WorkspaceRequest, spec: Any) -> Any:
    """The chosen connection, scoped. Blank means "every matching platform"."""
    from apps.flows.compat import installed_model

    raw_id = (request.POST.get("channel_connection") or "").strip()
    if not raw_id or not spec.bindable:
        return None
    model = installed_model("channels", "apps.channels", "ChannelConnection")
    if model is None:  # pragma: no cover
        return None
    return get_scoped_object_or_404(model, request.workspace, pk=raw_id)


def _refuse_duplicate_ref(
    flow: Flow, trigger_type: str, config: dict[str, Any], exclude: Any = None
) -> HttpResponse | None:
    if trigger_type != TriggerType.REF_URL:
        return None
    ref = (config.get("ref") or "").strip()
    if ref and services.duplicate_refs(flow, ref, exclude=exclude):
        return toast_response(
            tone="error",
            title="That reference is taken",
            body=f"Another trigger in this workspace already uses “{ref}”.",
        )
    return None


def _refusal(error: services.TriggerValidationError) -> HttpResponse:
    first = error.issues[0].message if error.issues else str(error)
    return toast_response(tone="error", title="Trigger not saved", body=first)
