"""The analytics pages: gating, tenancy, ranges and what they render.

``tests/idor.py`` reaches these routes through the *victim's* ``workspace_id``,
where ``RBACMiddleware`` answers first. The sharper case is here — the attacker's
own workspace id paired with the victim's flow id, where
``get_scoped_object_or_404`` is the only thing in the way.
"""

from datetime import timedelta
from typing import Any

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.analytics.counters import bump
from apps.analytics.models import TrackingSettings
from apps.analytics.tests.conftest import ENTRY_NODE
from apps.flows.fixtures import graph_for
from apps.flows.tests.support import published_flow
from apps.members.roles import WorkspaceRole

pytestmark = pytest.mark.django_db


def overview_url(tenancy: Any) -> str:
    return reverse("analytics:overview", kwargs={"workspace_id": tenancy.workspace.pk})


def flow_url(tenancy: Any, flow: Any) -> str:
    return reverse("analytics:flow_detail", kwargs={"workspace_id": tenancy.workspace.pk, "flow_id": flow.pk})


def settings_url(tenancy: Any) -> str:
    return reverse("analytics:tracking_settings", kwargs={"workspace_id": tenancy.workspace.pk})


def update_url(tenancy: Any) -> str:
    return reverse("analytics:update_tracking_settings", kwargs={"workspace_id": tenancy.workspace.pk})


class TestOverview:
    def test_it_lists_flows_with_counters(self, tenancy: Any, client_for: Any, flow: Any) -> None:
        bump(workspace_id=tenancy.workspace.pk, flow_id=flow.pk, node_id=ENTRY_NODE, sent=4, clicked=1)

        response = client_for(tenancy.owner).get(overview_url(tenancy))

        assert response.status_code == 200
        assert flow.name in response.content.decode()

    def test_a_flow_with_no_activity_is_absent(self, tenancy: Any, client_for: Any, flow: Any) -> None:
        """The flow list already answers "what exists"; this page answers "what
        has been running", and a page of zeroes answers neither."""
        response = client_for(tenancy.owner).get(overview_url(tenancy))

        assert flow.name not in response.content.decode()

    def test_another_workspaces_flow_is_never_listed(self, tenancy: Any, other_tenancy: Any, client_for: Any) -> None:
        theirs = published_flow(other_tenancy.workspace, graph_for("send_message"), name="Rival funnel")
        bump(workspace_id=other_tenancy.workspace.pk, flow_id=theirs.pk, node_id=ENTRY_NODE, sent=9)

        response = client_for(tenancy.owner).get(overview_url(tenancy))

        assert "Rival funnel" not in response.content.decode()


class TestFlowDetail:
    def test_it_renders_totals_the_trend_and_the_node_table(self, tenancy: Any, client_for: Any, flow: Any) -> None:
        bump(workspace_id=tenancy.workspace.pk, flow_id=flow.pk, node_id=ENTRY_NODE, sent=10, delivered=8, clicked=2)

        response = client_for(tenancy.owner).get(flow_url(tenancy, flow))
        body = response.content.decode()

        assert response.status_code == 200
        assert ENTRY_NODE in body
        # CTR beside the click count: 2 of 10.
        assert "20.0%" in body

    def test_the_trend_is_zero_filled_across_the_whole_range(self, tenancy: Any, client_for: Any, flow: Any) -> None:
        """A chart drawn from only the days that have rows draws a line between
        two points a fortnight apart and calls it a trend."""
        bump(workspace_id=tenancy.workspace.pk, flow_id=flow.pk, node_id=ENTRY_NODE, sent=1)

        response = client_for(tenancy.owner).get(f"{flow_url(tenancy, flow)}?days=7")

        assert response.context["chart"]["labels"] == [
            (timezone.now().date() - timedelta(days=offset)).isoformat() for offset in range(6, -1, -1)
        ]
        assert response.context["chart"]["series"]["sent"] == [0, 0, 0, 0, 0, 0, 1]

    def test_an_out_of_range_days_value_falls_back_rather_than_erroring(
        self, tenancy: Any, client_for: Any, flow: Any
    ) -> None:
        response = client_for(tenancy.owner).get(f"{flow_url(tenancy, flow)}?days=99999")

        assert response.status_code == 200
        # Not one of the three offered ranges, so the picker shows the default.
        assert response.context["days"] == 30

    def test_a_node_since_deleted_from_the_graph_keeps_its_numbers(
        self, tenancy: Any, client_for: Any, flow: Any
    ) -> None:
        """The sends happened. A counter that vanished when somebody tidied the
        canvas would make the totals stop reconciling."""
        bump(workspace_id=tenancy.workspace.pk, flow_id=flow.pk, node_id="removed_node", sent=3)

        response = client_for(tenancy.owner).get(flow_url(tenancy, flow))

        assert "removed_node" in response.content.decode()

    def test_another_workspaces_flow_is_a_404(self, tenancy: Any, other_tenancy: Any, client_for: Any) -> None:
        """The attacker's own workspace id with the victim's flow id — the case
        the URL-walking sweep cannot reach."""
        theirs = published_flow(other_tenancy.workspace, graph_for("send_message"), name="Rival funnel")
        url = reverse(
            "analytics:flow_detail",
            kwargs={"workspace_id": tenancy.workspace.pk, "flow_id": theirs.pk},
        )

        assert client_for(tenancy.owner).get(url).status_code == 404


class TestPermissions:
    @pytest.mark.parametrize(
        "role", [WorkspaceRole.ADMIN, WorkspaceRole.EDITOR, WorkspaceRole.AGENT, WorkspaceRole.VIEWER]
    )
    def test_every_role_holding_view_analytics_may_read(
        self, tenancy: Any, client_for: Any, flow: Any, role: str
    ) -> None:
        client = client_for(tenancy.user_for(role))

        assert client.get(overview_url(tenancy)).status_code == 200
        assert client.get(flow_url(tenancy, flow)).status_code == 200

    @pytest.mark.parametrize("role", [WorkspaceRole.AGENT, WorkspaceRole.VIEWER])
    def test_the_toggles_need_workspace_settings(self, tenancy: Any, client_for: Any, role: str) -> None:
        """Reading numbers and changing how mail is sent are different rights."""
        client = client_for(tenancy.user_for(role))

        assert client.get(settings_url(tenancy)).status_code == 403
        assert client.post(update_url(tenancy), {"open_pixel": "on"}).status_code == 403

    def test_a_signed_out_visitor_is_sent_to_the_login_page(self, client: Any, tenancy: Any) -> None:
        response = client.get(overview_url(tenancy))

        assert response.status_code == 302
        assert "/accounts/login/" in response["Location"]


class TestTrackingSettings:
    def test_both_toggles_start_off(self, tenancy: Any, client_for: Any) -> None:
        response = client_for(tenancy.owner).get(settings_url(tenancy))

        assert response.context["wrap_email_links"] is False
        assert response.context["open_pixel"] is False

    def test_saving_creates_the_row_on_first_use(self, tenancy: Any, client_for: Any) -> None:
        client_for(tenancy.owner).post(update_url(tenancy), {"wrap_email_links": "on"})

        row = TrackingSettings.objects.get(workspace=tenancy.workspace)
        assert (row.wrap_email_links, row.open_pixel) == (True, False)

    def test_an_unchecked_box_turns_the_toggle_off(self, tenancy: Any, client_for: Any) -> None:
        """An HTML checkbox posts nothing when it is unchecked, so "absent" has
        to mean off rather than "leave it alone"."""
        TrackingSettings.objects.create(workspace=tenancy.workspace, wrap_email_links=True, open_pixel=True)

        client_for(tenancy.owner).post(update_url(tenancy), {"open_pixel": "on"})

        row = TrackingSettings.objects.get(workspace=tenancy.workspace)
        assert (row.wrap_email_links, row.open_pixel) == (False, True)

    def test_another_workspaces_settings_are_untouched(self, tenancy: Any, other_tenancy: Any, client_for: Any) -> None:
        TrackingSettings.objects.create(workspace=other_tenancy.workspace, open_pixel=True)

        client_for(tenancy.owner).post(update_url(tenancy), {})

        assert TrackingSettings.objects.get(workspace=other_tenancy.workspace).open_pixel is True


class TestDashboard:
    def test_the_kpi_cards_render(self, tenancy: Any, client_for: Any, flow: Any) -> None:
        url = reverse("workspaces:dashboard", kwargs={"workspace_id": tenancy.workspace.pk})

        response = client_for(tenancy.owner).get(url)

        assert response.status_code == 200
        assert response.context["kpis"]["contacts_total"] == 0
        assert response.context["kpis"]["active_flows"] == 1
