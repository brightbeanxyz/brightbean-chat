"""Reading the counters back: ranges, deliverability and the dashboard's numbers."""

from datetime import timedelta
from typing import Any

import pytest
from django.utils import timezone

from apps.analytics import selectors
from apps.analytics.counters import bump
from apps.analytics.tests.conftest import ENTRY_NODE, TEXT, make_execution
from apps.channels.tests.fake_adapter import registered
from apps.common.platforms import Platform
from apps.flows.messaging import message_idempotency_key
from apps.messaging import services

pytestmark = pytest.mark.django_db


class TestResolveRange:
    def test_no_value_means_all_time(self) -> None:
        assert selectors.resolve_range(None).unbounded

    def test_a_default_applies_only_when_the_value_is_unusable(self) -> None:
        assert selectors.resolve_range("nonsense", default=30).start is not None
        assert selectors.resolve_range("7", default=30).start == timezone.now().date() - timedelta(days=6)

    def test_it_is_clamped_at_both_ends(self) -> None:
        """A ``?days=`` from a URL bar is untrusted, and an unbounded one is an
        unbounded scan."""
        assert selectors.resolve_range("0").unbounded
        assert selectors.resolve_range("-5").unbounded
        window = selectors.resolve_range("100000")
        assert window.start == timezone.now().date() - timedelta(days=selectors.MAX_DAYS - 1)

    def test_a_range_is_inclusive_of_both_ends(self) -> None:
        window = selectors.resolve_range("1")
        assert window.start == window.end == timezone.now().date()


class TestFlowReads:
    def test_totals_and_node_stats_agree(self, tenancy: Any, flow: Any) -> None:
        bump(workspace_id=tenancy.workspace.pk, flow_id=flow.pk, node_id="a", sent=2, clicked=1)
        bump(workspace_id=tenancy.workspace.pk, flow_id=flow.pk, node_id="b", sent=3, failed=1)
        window = selectors.resolve_range(None)

        totals = selectors.flow_totals(tenancy.workspace, flow.pk, window=window)
        nodes = selectors.flow_node_stats(tenancy.workspace, flow.pk, window=window)

        assert totals["sent"] == sum(row["sent"] for row in nodes.values()) == 5
        assert totals == {"sent": 5, "delivered": 0, "failed": 1, "clicked": 1}

    def test_a_range_excludes_older_rows(self, tenancy: Any, flow: Any) -> None:
        bump(
            workspace_id=tenancy.workspace.pk,
            flow_id=flow.pk,
            node_id="a",
            day=timezone.now().date() - timedelta(days=10),
            sent=5,
        )
        bump(workspace_id=tenancy.workspace.pk, flow_id=flow.pk, node_id="a", sent=1)

        window = selectors.resolve_range("7")
        assert selectors.flow_totals(tenancy.workspace, flow.pk, window=window)["sent"] == 1

    def test_the_series_is_zero_filled_and_ordered_oldest_first(self, tenancy: Any, flow: Any) -> None:
        bump(workspace_id=tenancy.workspace.pk, flow_id=flow.pk, node_id="a", sent=2)

        series = selectors.flow_daily_series(tenancy.workspace, flow.pk, window=selectors.resolve_range("3"))

        assert [row["sent"] for row in series] == [0, 0, 2]
        assert [row["date"] for row in series] == sorted(row["date"] for row in series)

    def test_workspace_flow_rows_are_busiest_first(self, tenancy: Any, flow: Any) -> None:
        from apps.flows.fixtures import graph_for
        from apps.flows.tests.support import published_flow

        quiet = published_flow(tenancy.workspace, graph_for("send_message"), name="Quiet")
        bump(workspace_id=tenancy.workspace.pk, flow_id=flow.pk, node_id="a", sent=9)
        bump(workspace_id=tenancy.workspace.pk, flow_id=quiet.pk, node_id="a", sent=1)

        rows = selectors.workspace_flow_rows(tenancy.workspace, window=selectors.resolve_range(None))

        assert [row["flow"].pk for row in rows] == [flow.pk, quiet.pk]

    def test_node_clicks_is_all_time(self, tenancy: Any, flow: Any) -> None:
        """A broadcast is a single event; a date filter over its numbers would
        only ever hide part of one."""
        bump(
            workspace_id=tenancy.workspace.pk,
            flow_id=flow.pk,
            node_id="broadcast",
            day=timezone.now().date() - timedelta(days=200),
            clicked=4,
        )

        assert selectors.node_clicks(tenancy.workspace, flow.pk, "broadcast") == 4


class TestDeliverability:
    def test_it_counts_every_outbound_message_on_a_connection(
        self, tenancy: Any, contact: Any, connection: Any, identity: Any, flow: Any
    ) -> None:
        """Read from ``message``, not from ``node_stat_daily``: a connection's
        deliverability is a property of everything it sent, agent replies and API
        sends included."""
        execution = make_execution(flow, contact, connection)
        with registered(Platform.TELEGRAM):
            services.send_outbound(
                workspace=tenancy.workspace,
                contact=contact,
                connection=connection,
                outbound=TEXT,
                source="automation",
                idempotency_key=message_idempotency_key(execution, ENTRY_NODE),
            )
            services.send_outbound(
                workspace=tenancy.workspace,
                contact=contact,
                connection=connection,
                outbound=TEXT,
                source="agent",
                idempotency_key="agent:1",
            )

        rows = selectors.connection_deliverability(tenancy.workspace, window=selectors.resolve_range(None))

        assert len(rows) == 1
        assert rows[0]["total"] == 2
        assert rows[0]["sent"] == 2
        assert rows[0]["connection"].pk == connection.pk

    def test_a_rate_with_no_denominator_is_none_rather_than_zero(
        self, tenancy: Any, contact: Any, connection: Any, flow: Any
    ) -> None:
        """No identity, so the send is refused. Nothing reached a provider, so
        there is nothing to express a delivery rate against."""
        execution = make_execution(flow, contact, connection)
        with registered(Platform.TELEGRAM):
            services.send_outbound(
                workspace=tenancy.workspace,
                contact=contact,
                connection=connection,
                outbound=TEXT,
                source="automation",
                idempotency_key=message_idempotency_key(execution, ENTRY_NODE),
            )

        rows = selectors.connection_deliverability(tenancy.workspace, window=selectors.resolve_range(None))

        assert rows[0]["failed"] == 1
        assert rows[0]["delivery_rate"] is None

    def test_another_workspaces_connections_are_absent(
        self, tenancy: Any, other_tenancy: Any, contact: Any, connection: Any, identity: Any, flow: Any
    ) -> None:
        rows = selectors.connection_deliverability(other_tenancy.workspace, window=selectors.resolve_range(None))

        assert rows == []


class TestDashboardKpis:
    def test_it_counts_contacts_flows_and_the_week_s_messages(
        self, tenancy: Any, contact: Any, connection: Any, identity: Any, flow: Any
    ) -> None:
        execution = make_execution(flow, contact, connection)
        with registered(Platform.TELEGRAM):
            services.send_outbound(
                workspace=tenancy.workspace,
                contact=contact,
                connection=connection,
                outbound=TEXT,
                source="automation",
                idempotency_key=message_idempotency_key(execution, ENTRY_NODE),
            )

        kpis = selectors.dashboard_kpis(tenancy.workspace)

        assert kpis["contacts_total"] == 1
        assert kpis["contacts_new"] == 1
        assert kpis["messages_out"] == 1
        assert kpis["messages_in"] == 0
        assert kpis["active_flows"] == 1

    def test_another_workspaces_rows_are_never_counted(
        self, tenancy: Any, other_tenancy: Any, contact: Any, flow: Any
    ) -> None:
        kpis = selectors.dashboard_kpis(other_tenancy.workspace)

        assert kpis["contacts_total"] == 0
        assert kpis["active_flows"] == 0
