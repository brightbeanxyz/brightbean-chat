"""The builder data API (SPEC §16) — session-authenticated, workspace-scoped, JSON.

**The URL deviates from SPEC §16 on purpose, and L3-C should read this.** The
spec writes these as ``/api/flows/<id>/``; they are mounted at
``/w/<uuid:workspace_id>/api/flows/<uuid:flow_id>/`` instead. The kwarg name
``workspace_id`` is ``RBACMiddleware``'s entire resolution contract: without it
``request.workspace`` is ``None``, ``require_permission`` refuses every call, and
the view would have to look the flow up across tenants and hand-roll the
membership check that the middleware and decorators already do everywhere else
in this codebase. A second, bespoke authorisation path on the app's newest
endpoints is a worse trade than a longer URL, and the sweep in ``tests/idor.py``
covers the routes automatically as a consequence.

Authentication is the session, and **CSRF is enforced** — no ``csrf_exempt``
anywhere here (SECURITY-BASELINE §8). The builder sends ``X-CSRFToken``; the
edit page sets the cookie.

Reads are open to any workspace member and writes require ``edit_flows``, which
is what "Editor+ required; Viewer read-only" means given that ``edit_flows`` is
held by Admin and Editor alone and there is no read-only flows key in
``apps.members.roles``. Agent is read-only here for the same reason.

Sizes are checked before parsing, and parsing before anything touches the
database (SECURITY-BASELINE §7).
"""

import json
from typing import Any

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.common.shortcuts import get_scoped_object_or_404
from apps.flows import analytics, services
from apps.flows.models import Flow, Trigger
from apps.flows.picklists import picklists
from apps.flows.schema import MAX_GRAPH_BYTES, empty_graph, json_schema, limits
from apps.flows.triggers import services as trigger_services
from apps.members.decorators import require_permission, require_workspace_role
from apps.members.requests import WorkspaceRequest
from apps.members.roles import WorkspaceRole

__all__ = [
    "flow_detail",
    "flow_publish",
    "flow_rename",
    "flow_schema",
    "flow_stats",
    "flow_take_offline",
    "flow_trigger_enabled",
]

#: The longest name a flow may have — the model's own column width.
MAX_NAME = Flow._meta.get_field("name").max_length or 200

# Viewer is the lowest workspace role, so this reads as "any member of this
# workspace". Spelled with the role rather than a bare membership check so the
# gate is the same mechanism every other view uses.
require_workspace_member = require_workspace_role(WorkspaceRole.VIEWER)

# The graph cap plus room for the envelope around it. Enforced on the raw body
# before json.loads, so an oversized document costs a length check rather than a
# parse.
MAX_REQUEST_BYTES = MAX_GRAPH_BYTES + 4096


def _get_flow(request: WorkspaceRequest, flow_id: str) -> Flow:
    """The flow, or 404 — including when it belongs to another workspace."""
    return get_scoped_object_or_404(Flow, request.workspace, pk=flow_id)


def _flow_payload(flow: Flow) -> dict[str, Any]:
    return {
        "id": str(flow.pk),
        "name": flow.name,
        "status": flow.status,
        "folder": flow.folder,
        "updated_at": flow.updated_at.isoformat(),
    }


def _error(code: str, message: str, status: int, **extra: Any) -> JsonResponse:
    return JsonResponse({"error": {"code": code, "message": message}, **extra}, status=status)


def _read_json_object(request: WorkspaceRequest) -> dict[str, Any] | JsonResponse:
    """Parse a JSON object body under the size cap, or return the error response.

    ``Content-Length`` is checked first because it is free, then the body itself,
    because a chunked request has no ``Content-Length`` to check.
    """
    declared = request.META.get("CONTENT_LENGTH") or 0
    try:
        declared = int(declared)
    except (TypeError, ValueError):
        declared = 0
    if declared > MAX_REQUEST_BYTES:
        return _error("payload_too_large", f"The request exceeds {MAX_REQUEST_BYTES} bytes.", 413)

    try:
        body = request.body
    except ValueError:
        # Django re-parses CONTENT_LENGTH inside request.body and lets the
        # ValueError escape, so a header of "twelve" is a 500 on any view that
        # reads a body. A malformed header is a malformed request.
        return _error("malformed_request", "The Content-Length header is not a number.", 400)

    if len(body) > MAX_REQUEST_BYTES:
        return _error("payload_too_large", f"The request exceeds {MAX_REQUEST_BYTES} bytes.", 413)

    try:
        payload = json.loads(body or b"{}")
    except (ValueError, RecursionError):
        # RecursionError is belt and braces: CPython's json parser handles
        # deeper nesting than the graph depth cap allows, so in practice the cap
        # catches those first — but a parser that gives up must not be a 500.
        return _error("malformed_json", "The request body is not valid JSON.", 400)

    if not isinstance(payload, dict):
        return _error("malformed_json", "The request body must be a JSON object.", 400)
    return payload


# ---------------------------------------------------------------------------
# GET / PUT /w/<workspace_id>/api/flows/<flow_id>/
# ---------------------------------------------------------------------------


@login_required
@require_http_methods(["GET", "PUT"])
def flow_detail(request: WorkspaceRequest, workspace_id: str, flow_id: str) -> HttpResponse:
    """Read the draft, or save it.

    The two methods carry different permissions, which is why the gate is on the
    handlers rather than on this function: the usual
    ``@login_required`` → ``@require_permission`` → method stacking cannot express
    "read as a member, write as an editor" on one URL.
    """
    if request.method == "GET":
        return _detail_read(request, workspace_id, flow_id)
    return _detail_save(request, workspace_id, flow_id)


@require_workspace_member
def _detail_read(request: WorkspaceRequest, workspace_id: str, flow_id: str) -> HttpResponse:
    flow = _get_flow(request, flow_id)
    draft = services.latest_version(flow)
    published = services.published_version(flow)
    # empty_graph(), not {}: a flow with no version is still described by the
    # schema this same response links to, and {} fails it — the client would get
    # three envelope errors about a graph nobody authored.
    graph = draft.graph_json if draft else empty_graph()
    result = services.validate_for_workspace(graph, request.workspace, flow=flow)
    return JsonResponse(
        {
            "flow": _flow_payload(flow),
            "version": draft.as_dict() if draft else None,
            "graph": graph,
            "published_version": published.as_dict() if published else None,
            "picklists": picklists(request.workspace),
            # Read-only. The builder does not edit triggers — the HTMX panel on
            # the same page does — so this is a summary and never the raw
            # config, which would make the React store a second place a
            # trigger's configuration lives. Always present, empty list
            # included: TestPicklists pins that rule for picklists and it is the
            # same rule, so a client never branches on a key being absent.
            "triggers": trigger_services.summaries(flow),
            "validation": result.as_dict(),
            "limits": limits(),
            "schema_url": reverse("flows:api_schema", kwargs={"workspace_id": workspace_id}),
        }
    )


@require_permission("edit_flows")
def _detail_save(request: WorkspaceRequest, workspace_id: str, flow_id: str) -> HttpResponse:
    payload = _read_json_object(request)
    if isinstance(payload, JsonResponse):
        return payload

    if "graph" not in payload:
        return _error("missing_graph", 'The body must be {"graph": {...}}.', 400)

    flow = _get_flow(request, flow_id)
    # The body was measured on the way in and Django caches it, so this is free
    # — and the graph is a subtree of that body, so it is a valid upper bound
    # for the size cap. It saves re-serialising the whole graph on every
    # two-second autosave.
    result = services.validate_for_workspace(
        payload["graph"], request.workspace, known_size=len(request.body), flow=flow
    )
    if result.blocks_save:
        # Structural findings only: the document is too big, too deep, or
        # carries a key no node type declares. A half-wired graph — a dangling
        # edge, no entry node — is an ordinary autosave and saves below.
        return JsonResponse({"validation": result.as_dict()}, status=422)

    version = services.save_draft(flow, payload["graph"], user=request.user)
    return JsonResponse({"flow": _flow_payload(flow), "version": version.as_dict(), "validation": result.as_dict()})


# ---------------------------------------------------------------------------
# POST /w/<workspace_id>/api/flows/<flow_id>/publish/
# ---------------------------------------------------------------------------


@login_required
@require_permission("edit_flows")
@require_POST
def flow_publish(request: WorkspaceRequest, workspace_id: str, flow_id: str) -> HttpResponse:
    """Validate strictly and publish. Errors block; warnings do not."""
    flow = _get_flow(request, flow_id)
    try:
        published = services.publish(flow, user=request.user)
    except services.FlowValidationError as exc:
        return JsonResponse({"validation": exc.result.as_dict()}, status=422)
    except services.FlowPlanLimitError as exc:
        # Caught here, not left to the default handler: that answers in HTML,
        # and the builder reads a non-JSON response as an expired session. The
        # message already says what to do ("switch one off to free up a slot").
        return _error("plan_limit", str(exc), 409)
    # publish() hands back the findings it validated against, from inside the
    # transaction that approved them. Re-running validation here would repeat
    # the whole walk and could answer differently, since a channel could change
    # between the commit and the second pass.
    return JsonResponse(
        {
            "flow": _flow_payload(flow),
            "version": published.version.as_dict(),
            "triggers": trigger_services.summaries(flow),
            "validation": published.validation.as_dict(),
        }
    )


# ---------------------------------------------------------------------------
# POST /w/<workspace_id>/api/flows/<flow_id>/rename/
# ---------------------------------------------------------------------------


@login_required
@require_permission("edit_flows")
@require_POST
def flow_rename(request: WorkspaceRequest, workspace_id: str, flow_id: str) -> HttpResponse:
    """Rename the flow from the builder's top bar, where its name is edited in
    place (HANDOFF §3, Flow builder). The same service the list's Rename uses.

    ``{"name": "..."}``. A blank name is refused rather than saved: the list and
    every picker that offers this flow would otherwise show nothing at all.
    """
    payload = _read_json_object(request)
    if isinstance(payload, JsonResponse):
        return payload
    name = payload.get("name")
    if not isinstance(name, str) or not name.strip():
        return _error("missing_name", "A flow needs a name.", 400)
    flow = _get_flow(request, flow_id)
    services.rename_flow(flow, name.strip()[:MAX_NAME])
    return JsonResponse({"flow": _flow_payload(flow)})


# ---------------------------------------------------------------------------
# POST /w/<workspace_id>/api/flows/<flow_id>/triggers/<trigger_id>/enabled/
# ---------------------------------------------------------------------------


@login_required
@require_permission("edit_flows")
@require_POST
def flow_trigger_enabled(request: WorkspaceRequest, workspace_id: str, flow_id: str, trigger_id: str) -> HttpResponse:
    """Switch one trigger on or off from the builder's outline.

    ``{"enabled": true}`` — the state to end in, not a toggle. The drawer's
    ``flows:trigger_toggle`` flips whatever is stored, which is right for a
    control that re-renders from the server on every change; the outline's
    switch shows what the page last loaded, and a flip from a stale page would
    turn off the trigger somebody in another tab had just turned on.

    Answers with the trigger summaries, the same list the detail read carries,
    so the builder applies it the way it applies any other refresh.
    """
    payload = _read_json_object(request)
    if isinstance(payload, JsonResponse):
        return payload
    enabled = payload.get("enabled")
    if not isinstance(enabled, bool):
        return _error("missing_enabled", 'The body must be {"enabled": true} or {"enabled": false}.', 400)
    flow = _get_flow(request, flow_id)
    trigger = get_scoped_object_or_404(Trigger, request.workspace, pk=trigger_id, flow=flow)
    trigger_services.set_enabled(trigger, enabled)
    return JsonResponse({"triggers": trigger_services.summaries(flow)})


# ---------------------------------------------------------------------------
# POST /w/<workspace_id>/api/flows/<flow_id>/offline/
# ---------------------------------------------------------------------------


@login_required
@require_permission("edit_flows")
@require_POST
def flow_take_offline(request: WorkspaceRequest, workspace_id: str, flow_id: str) -> HttpResponse:
    """Take a live flow offline and stop the conversations in it.

    Answers with the same pieces the publish response carries, so the builder
    applies either one the same way: the flow's new status, the latest version
    (no longer flagged published) and the triggers, plus how many contacts'
    conversations were stopped so the confirmation can say so.
    """
    flow = _get_flow(request, flow_id)
    try:
        stopped = services.take_offline(flow)
    except services.FlowNotLiveError as exc:
        return _error("not_live", str(exc), 409)
    latest = services.latest_version(flow)
    return JsonResponse(
        {
            "flow": _flow_payload(flow),
            "version": latest.as_dict() if latest else None,
            "triggers": trigger_services.summaries(flow),
            "stopped": stopped,
        }
    )


# ---------------------------------------------------------------------------
# GET /w/<workspace_id>/api/flows/<flow_id>/stats/
# ---------------------------------------------------------------------------


@login_required
@require_permission("view_analytics")
@require_GET
def flow_stats(request: WorkspaceRequest, workspace_id: str, flow_id: str) -> HttpResponse:
    """Per-node counters for the stats overlay (SPEC §5's ``node_stat_daily``).

    **The shape is unchanged from the stub L2-D shipped**, which is the point:
    L3-C's overlay was written against it and issue #26 fills it rather than
    rewriting it. ``available`` still means "there is something behind this" — it
    now says whether ``apps.analytics`` is installed rather than whether L7-A has
    merged, and the builder still renders that state distinctly rather than as a
    row of zeros.

    ``?days=N`` is additive and optional. Omitted — which is what the overlay
    sends — the counters are all-time, because the chips beside a node are bare
    cumulative numbers with no range control next to them. The flow stats page
    asks for 7, 30 or 90 explicitly.

    Gated on ``view_analytics`` rather than on bare workspace membership, per the
    permission this issue owns. Every workspace role holds that key
    (``apps.members.roles``), so no reader loses access; what changes is that the
    gate now names the thing being read.
    """
    flow = _get_flow(request, flow_id)
    stats = analytics.stats(request.workspace, flow.pk, days=request.GET.get("days"))
    if stats is None:
        return JsonResponse(
            {
                "flow": {"id": str(flow.pk)},
                "available": False,
                "nodes": {},
                "totals": {"sent": 0, "delivered": 0, "failed": 0, "clicked": 0},
            }
        )
    return JsonResponse({"flow": {"id": str(flow.pk)}, "available": True, **stats})


# ---------------------------------------------------------------------------
# GET /w/<workspace_id>/api/flows/schema/
# ---------------------------------------------------------------------------


@login_required
@require_workspace_member
@require_GET
def flow_schema(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """The node-config JSON Schema, served live.

    The same document is committed at ``static/flows/flow-schema.json`` for the
    bundle to import at build time; this endpoint is the runtime copy, generated
    by the same function, so a deployment cannot serve a schema that disagrees
    with the one validating its saves.
    """
    return JsonResponse(json_schema())
