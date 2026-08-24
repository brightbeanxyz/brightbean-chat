"""Reading the counters back (SPEC §18).

Every query here is workspace-scoped and every one of them is a `SUM` over
``node_stat_daily`` or a `COUNT` over rows another app owns. Nothing in this
module writes, and nothing recomputes a number some other app already keeps —
SPEC §13.2's broadcast figures come from ``broadcast.stats`` through
``apps.broadcasts.services.counters``, and this module reads *that* rather than
counting recipients a second way.

Two date conventions worth knowing:

* ``days=None`` means **all time**. That is the default the builder's stats
  overlay gets, because its chips are bare cumulative counts with no range
  control next to them; the flow page asks for 7, 30 or 90 explicitly.
* the range is inclusive of both ends and measured in **UTC days**, matching the
  bucket :class:`apps.analytics.models.NodeStatDaily` is written in.
"""

from dataclasses import dataclass
from datetime import date as date_type
from datetime import timedelta
from typing import Any

from django.db.models import Count, Q, Sum
from django.utils import timezone

from apps.analytics.models import COUNTER_FIELDS, NodeStatDaily

__all__ = [
    "RANGE_CHOICES",
    "DateRange",
    "connection_deliverability",
    "dashboard_kpis",
    "empty_counters",
    "flow_daily_series",
    "flow_node_stats",
    "flow_totals",
    "node_clicks",
    "resolve_range",
    "workspace_flow_rows",
]

#: The ranges the flow page offers, in days. SPEC's own wording for the trend.
RANGE_CHOICES: tuple[int, ...] = (7, 30, 90)

#: How many days a caller may ask for. A year of daily rows is the most any of
#: these pages renders, and an unbounded ``?days=`` is an unbounded scan.
MAX_DAYS = 366


@dataclass(frozen=True)
class DateRange:
    """An inclusive UTC day range, or all of time when both ends are ``None``."""

    start: date_type | None = None
    end: date_type | None = None

    @property
    def unbounded(self) -> bool:
        return self.start is None and self.end is None


def empty_counters() -> dict[str, int]:
    """A zeroed counter dict, in SPEC §5's field order."""
    return dict.fromkeys(COUNTER_FIELDS, 0)


def resolve_range(days: Any, *, default: int | None = None) -> DateRange:
    """Turn an untrusted ``?days=`` into a bounded range.

    Anything unparseable falls back to ``default``, and ``default=None`` means
    all time. Clamped at both ends: zero or negative is one day, and anything
    past :data:`MAX_DAYS` is :data:`MAX_DAYS`.
    """
    try:
        value = int(days)
    except (TypeError, ValueError):
        value = default if default is not None else 0
    if value <= 0:
        return DateRange()
    value = min(value, MAX_DAYS)
    today = timezone.now().date()
    return DateRange(start=today - timedelta(days=value - 1), end=today)


def _rows(workspace: Any, window: DateRange) -> Any:
    rows = NodeStatDaily.objects.for_workspace(workspace)
    if window.start is not None:
        rows = rows.filter(date__gte=window.start)
    if window.end is not None:
        rows = rows.filter(date__lte=window.end)
    return rows


def _sums() -> dict[str, Any]:
    return {field: Sum(field) for field in COUNTER_FIELDS}


def _counters_from(row: dict[str, Any]) -> dict[str, int]:
    return {field: int(row.get(field) or 0) for field in COUNTER_FIELDS}


def flow_totals(workspace: Any, flow_id: Any, *, window: DateRange) -> dict[str, int]:
    """One flow's headline counters over ``window``."""
    aggregate = _rows(workspace, window).filter(flow_id=flow_id).aggregate(**_sums())
    return _counters_from(aggregate)


def flow_node_stats(workspace: Any, flow_id: Any, *, window: DateRange) -> dict[str, dict[str, int]]:
    """One flow's counters per node, keyed by node id — the overlay's payload."""
    grouped = _rows(workspace, window).filter(flow_id=flow_id).values("node_id").annotate(**_sums())
    return {str(row["node_id"]): _counters_from(row) for row in grouped}


def node_clicks(workspace: Any, flow_id: Any, node_id: str) -> int:
    """All-time clicks on one node — what a broadcast's own stats tile shows.

    All-time rather than ranged, because a broadcast is a single event: its
    numbers are the numbers of that send, and a date filter over them would only
    ever hide part of one.
    """
    aggregate = (
        NodeStatDaily.objects.for_workspace(workspace)
        .filter(flow_id=flow_id, node_id=node_id)
        .aggregate(total=Sum("clicked"))
    )
    return int(aggregate["total"] or 0)


def flow_daily_series(workspace: Any, flow_id: Any, *, window: DateRange) -> list[dict[str, Any]]:
    """One row per day in ``window``, zero-filled, oldest first.

    Zero-filled here rather than in the template: a chart drawn from only the
    days that have rows draws a line between two points a fortnight apart and
    calls it a trend.
    """
    grouped = {
        row["date"]: _counters_from(row)
        for row in _rows(workspace, window).filter(flow_id=flow_id).values("date").annotate(**_sums())
    }
    if window.start is None or window.end is None:
        return [{"date": day, **counters} for day, counters in sorted(grouped.items())]

    series: list[dict[str, Any]] = []
    day = window.start
    while day <= window.end:
        series.append({"date": day, **grouped.get(day, empty_counters())})
        day += timedelta(days=1)
    return series


def workspace_flow_rows(workspace: Any, *, window: DateRange) -> list[dict[str, Any]]:
    """Every flow that has counters in ``window``, busiest first.

    Flows with no activity are left out rather than listed at zero: the overview
    answers "what has been running", and the flow list already answers "what
    exists".
    """
    from apps.flows.models import Flow

    grouped = _rows(workspace, window).values("flow_id").annotate(**_sums())
    totals = {row["flow_id"]: _counters_from(row) for row in grouped}
    if not totals:
        return []

    names = {
        flow.pk: flow
        for flow in Flow.objects.for_workspace(workspace).filter(pk__in=list(totals)).only("id", "name", "status")
    }
    rows: list[dict[str, Any]] = [
        {"flow": names[flow_id], **counters}
        for flow_id, counters in totals.items()
        # A flow deleted between the two queries, or one whose counters outlived
        # it — the FK cascades, so this is a race rather than a state.
        if flow_id in names
    ]
    rows.sort(key=lambda row: (-int(row["sent"]), str(row["flow"].name).lower()))
    return rows


def connection_deliverability(workspace: Any, *, window: DateRange) -> list[dict[str, Any]]:
    """Per-connection send outcomes — SPEC §13.2's "deliverability summary".

    Read from ``message`` rather than from ``node_stat_daily``, and deliberately:
    a connection's deliverability is a property of everything it sent, including
    agent replies and API sends that belong to no flow node. ``node_stat_daily``
    is for flow nodes and would answer a narrower question than the one this
    table's heading asks.
    """
    from apps.channels.models import ChannelConnection
    from apps.messaging.models import Message, MessageDirection, MessageStatus

    rows = Message.objects.for_workspace(workspace).filter(direction=MessageDirection.OUT, internal=False)
    if window.start is not None:
        rows = rows.filter(created_at__date__gte=window.start)
    if window.end is not None:
        rows = rows.filter(created_at__date__lte=window.end)

    grouped = {
        row["channel_connection_id"]: row
        for row in rows.values("channel_connection_id").annotate(
            total=Count("id"),
            # "Reached the provider" — anything that climbed past `queued`
            # without failing. `deleted` is not on the ladder and is excluded on
            # purpose: the row was retracted, not sent badly.
            sent=Count("id", filter=Q(status__in=(MessageStatus.SENT, MessageStatus.DELIVERED, MessageStatus.READ))),
            delivered=Count("id", filter=Q(status__in=(MessageStatus.DELIVERED, MessageStatus.READ))),
            failed=Count("id", filter=Q(status=MessageStatus.FAILED)),
        )
    }
    if not grouped:
        return []

    connections = {
        connection.pk: connection
        for connection in ChannelConnection.objects.for_workspace(workspace).filter(pk__in=list(grouped))
    }
    summary: list[dict[str, Any]] = []
    for connection_id, row in grouped.items():
        connection = connections.get(connection_id)
        if connection is None:
            continue
        sent = int(row["sent"])
        summary.append(
            {
                "connection": connection,
                "total": int(row["total"]),
                "sent": sent,
                "delivered": int(row["delivered"]),
                "failed": int(row["failed"]),
                "delivery_rate": round(100 * int(row["delivered"]) / sent, 1) if sent else None,
            }
        )
    summary.sort(key=lambda row: (-int(row["total"]), str(row["connection"].display_name).lower()))
    return summary


def dashboard_kpis(workspace: Any) -> dict[str, Any]:
    """The workspace landing page's cards.

    Six numbers over three apps, each one a single aggregate. Deliberately not
    cached: they are counts over indexed columns, the page is not hot, and a
    cached KPI that disagrees with the page it links to is worse than a query.
    """
    from apps.broadcasts.models import Broadcast
    from apps.contacts.models import Contact, ContactStatus
    from apps.flows.models import Flow, FlowStatus
    from apps.messaging.models import Message, MessageDirection

    now = timezone.now()
    week_ago = now - timedelta(days=7)

    contacts = Contact.objects.for_workspace(workspace).filter(status=ContactStatus.ACTIVE)
    messages = Message.objects.for_workspace(workspace).filter(created_at__gte=week_ago, internal=False)
    counted = messages.aggregate(
        inbound=Count("id", filter=Q(direction=MessageDirection.IN)),
        outbound=Count("id", filter=Q(direction=MessageDirection.OUT)),
    )
    return {
        "contacts_total": contacts.count(),
        "contacts_new": contacts.filter(created_at__gte=week_ago).count(),
        "messages_in": int(counted["inbound"] or 0),
        "messages_out": int(counted["outbound"] or 0),
        "active_flows": Flow.objects.for_workspace(workspace).filter(status=FlowStatus.ACTIVE).count(),
        "recent_broadcasts": list(
            Broadcast.objects.for_workspace(workspace).select_related("channel_connection").order_by("-created_at")[:5]
        ),
    }
