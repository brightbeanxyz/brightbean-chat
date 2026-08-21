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
from apps.flows import services
from apps.flows.models import Flow
from apps.flows.picklists import picklists
from apps.flows.schema import (
    MAX_EDGES,
    MAX_GRAPH_BYTES,
    MAX_GRAPH_DEPTH,
    MAX_NODES,
    SCHEMA_VERSION,
    json_schema,
)
from apps.members.decorators import require_permission, require_workspace_role
from apps.members.requests import WorkspaceRequest
from apps.members.roles import WorkspaceRole

__all__ = ["flow_detail", "flow_publish", "flow_schema", "flow_stats"]

# Viewer is the lowest workspace role, so this reads as "any member of this
# workspace". Spelled with the role rather than a bare membership check so the
# gate is the same mechanism every other view uses.
require_workspace_member = require_workspace_role(WorkspaceRole.VIEWER)

# The graph cap plus room for the envelope around it. Enforced on the raw body
# before json.loads, so an oversized document costs a length check rather than a
# parse.
MAX_REQUEST_BYTES = MAX_GRAPH_BYTES + 4096

_LIMITS = {
    "max_graph_bytes": MAX_GRAPH_BYTES,
    "max_graph_depth": MAX_GRAPH_DEPTH,
    "max_nodes": MAX_NODES,
    "max_edges": MAX_EDGES,
    "schema_version": SCHEMA_VERSION,
}


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
    graph = draft.graph_json if draft else {}
    result = services.validate_for_workspace(graph, request.workspace)
    return JsonResponse(
        {
            "flow": _flow_payload(flow),
            "version": draft.as_dict() if draft else None,
            "graph": graph,
            "published_version": published.as_dict() if published else None,
            "picklists": picklists(request.workspace),
            "validation": result.as_dict(),
            "limits": _LIMITS,
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
    result = services.validate_for_workspace(payload["graph"], request.workspace)
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
        version = services.publish(flow, user=request.user)
    except services.FlowValidationError as exc:
        return JsonResponse({"validation": exc.result.as_dict()}, status=422)
    result = services.validate_for_workspace(version.graph_json, request.workspace)
    return JsonResponse({"flow": _flow_payload(flow), "version": version.as_dict(), "validation": result.as_dict()})


# ---------------------------------------------------------------------------
# GET /w/<workspace_id>/api/flows/<flow_id>/stats/
# ---------------------------------------------------------------------------


@login_required
@require_workspace_member
@require_GET
def flow_stats(request: WorkspaceRequest, workspace_id: str, flow_id: str) -> HttpResponse:
    """Per-node counters for the stats overlay — zeros until L7-A (issue #26).

    The shape is the real one (``node_stat_daily`` in SPEC §5 counts sent,
    delivered, failed and clicked), so L3-C can build the overlay against it now.
    ``available`` is false while there is nothing behind it; L7-A flips it.
    """
    flow = _get_flow(request, flow_id)
    return JsonResponse(
        {
            "flow": {"id": str(flow.pk)},
            "available": False,
            "nodes": {},
            "totals": {"sent": 0, "delivered": 0, "failed": 0, "clicked": 0},
        }
    )


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
