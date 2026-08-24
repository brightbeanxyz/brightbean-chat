"""The analytics pages (SPEC §18).

Three of them, all read-only except the last:

``overview``
    Every flow that has moved a counter in the range, and the per-connection
    deliverability summary. The section's landing page.
``flow_detail``
    SPEC §18's "flow detail page": headline totals, a 7/30/90-day trend, and the
    per-node table. Distinct from the *builder*, which shows the same counters as
    chips on the canvas — a table is what you read, a chip is what you glance at.
``tracking_settings``
    The two per-workspace email toggles, both off by default.

Everything reads is gated on ``view_analytics``, which every workspace role holds
(``apps.members.roles``) — it is the key SPEC §4.2 gives this section, and naming
it is what makes the gate legible. The toggles are workspace configuration, so
they answer to ``manage_workspace_settings`` instead.

Decorator order is the house one: ``login_required`` → the permission →
the method, so a request from another tenant answers 404 before it can learn that
a 405 would have been the alternative.
"""

from typing import Any

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from apps.analytics import selectors
from apps.analytics.models import TrackingSettings
from apps.common.shortcuts import get_scoped_object_or_404
from apps.flows.models import Flow
from apps.members.decorators import require_permission
from apps.members.requests import WorkspaceRequest

__all__ = ["flow_detail", "overview", "tracking_settings", "update_tracking_settings"]

#: What the overview and the flow page open on. Thirty days is the range that
#: shows a weekly rhythm without the first week of a new flow being the whole
#: chart.
DEFAULT_DAYS = 30


def _window(request: WorkspaceRequest) -> Any:
    """The range this request asks for, clamped. Untrusted input all the way."""
    return selectors.resolve_range(request.GET.get("days"), default=DEFAULT_DAYS)


def _days(request: WorkspaceRequest) -> int:
    """The chosen range as a number, for the range picker's active state."""
    try:
        value = int(request.GET.get("days") or "")
    except (TypeError, ValueError):
        return DEFAULT_DAYS
    return value if value in selectors.RANGE_CHOICES else DEFAULT_DAYS


@login_required
@require_permission("view_analytics")
@require_GET
def overview(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """Flows with activity, and how each connection is delivering."""
    window = _window(request)
    return render(
        request,
        "analytics/overview.html",
        {
            "days": _days(request),
            "range_choices": selectors.RANGE_CHOICES,
            "flow_rows": selectors.workspace_flow_rows(request.workspace, window=window),
            "connections": selectors.connection_deliverability(request.workspace, window=window),
        },
    )


@login_required
@require_permission("view_analytics")
@require_GET
def flow_detail(request: WorkspaceRequest, workspace_id: str, flow_id: str) -> HttpResponse:
    """One flow's headline totals, trend and per-node table.

    The node table is built from the *published* graph where there is one, so a
    node's row carries its type rather than a bare id — and counters for a node
    that has since been deleted still appear, because the send happened and the
    number is the record of it.
    """
    flow = get_scoped_object_or_404(Flow, request.workspace, pk=flow_id)
    window = _window(request)
    node_stats = selectors.flow_node_stats(request.workspace, flow.pk, window=window)
    totals = selectors.flow_totals(request.workspace, flow.pk, window=window)
    series = selectors.flow_daily_series(request.workspace, flow.pk, window=window)
    return render(
        request,
        "analytics/flow_detail.html",
        {
            "flow": flow,
            "days": _days(request),
            "range_choices": selectors.RANGE_CHOICES,
            "totals": totals,
            "click_rate": _rate(totals["clicked"], totals["sent"]),
            "chart": _chart(series),
            "node_rows": _node_rows(flow, node_stats),
        },
    )


def _rate(part: int, whole: int) -> float | None:
    """A percentage, or ``None`` where there is no denominator to divide by."""
    return round(100 * part / whole, 1) if whole else None


def _chart(series: list[dict[str, Any]]) -> dict[str, Any]:
    """The trend, shaped for ``json_script`` — dates as ISO strings, not objects.

    Handed to the page through ``{{ chart|json_script }}`` rather than
    interpolated into a script body: the page's CSP is nonce-based
    (SECURITY-BASELINE §8) and a JSON island is the way to get data across
    without an inline expression per value.
    """
    return {
        "labels": [row["date"].isoformat() for row in series],
        "series": {field: [row[field] for row in series] for field in ("sent", "delivered", "failed", "clicked")},
    }


def _node_rows(flow: Flow, node_stats: dict[str, dict[str, int]]) -> list[dict[str, Any]]:
    """One row per node with counters, labelled from the graph where it still exists."""
    from apps.flows import services

    version = services.published_version(flow) or services.latest_version(flow)
    graph = getattr(version, "graph_json", None) or {}
    nodes = graph.get("nodes") if isinstance(graph, dict) else None
    types = {
        str(node.get("id")): str(node.get("type") or "")
        for node in (nodes or ())
        if isinstance(node, dict) and node.get("id")
    }
    rows: list[dict[str, Any]] = [
        {
            "node_id": node_id,
            # Blank rather than "unknown": the node was deleted from the graph
            # and there is nothing honest to call it.
            "type": types.get(node_id, ""),
            "ctr": _rate(counters["clicked"], counters["sent"]),
            **counters,
        }
        for node_id, counters in node_stats.items()
    ]
    rows.sort(key=lambda row: (-int(row["sent"]), str(row["node_id"])))
    return rows


@login_required
@require_permission("manage_workspace_settings")
@require_GET
def tracking_settings(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """The two email toggles. Absent row means both off — see the model."""
    row = TrackingSettings.objects.filter(workspace=request.workspace).first()
    return render(
        request,
        "analytics/settings.html",
        {
            "wrap_email_links": bool(row and row.wrap_email_links),
            "open_pixel": bool(row and row.open_pixel),
        },
    )


@login_required
@require_permission("manage_workspace_settings")
@require_POST
def update_tracking_settings(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """Save the toggles, creating the row on first use."""
    TrackingSettings.objects.update_or_create(
        workspace=request.workspace,
        defaults={
            "wrap_email_links": bool(request.POST.get("wrap_email_links")),
            "open_pixel": bool(request.POST.get("open_pixel")),
        },
    )
    messages.success(request, "Email tracking settings saved.")
    return redirect(reverse("analytics:tracking_settings", kwargs={"workspace_id": workspace_id}))
