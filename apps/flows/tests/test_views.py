"""The flow list and the builder's host page."""

import pytest
from django.test import Client
from django.urls import reverse

from apps.flows.fixtures import graph_for
from apps.flows.models import Flow, FlowStatus
from apps.flows.services import archive_flow, create_flow, publish, save_draft
from apps.members.roles import WorkspaceRole

pytestmark = pytest.mark.django_db


def list_url(tenancy):
    return reverse("flows:list", kwargs={"workspace_id": tenancy.workspace.pk})


def action_url(name, tenancy, flow):
    return reverse(name, kwargs={"workspace_id": tenancy.workspace.pk, "flow_id": flow.pk})


class TestTheList:
    def test_it_shows_the_workspaces_flows(self, tenancy, client_for):
        create_flow(workspace=tenancy.workspace, name="Welcome series")

        response = client_for(tenancy.owner).get(list_url(tenancy))

        assert response.status_code == 200
        assert "Welcome series" in response.content.decode()

    def test_another_workspaces_flows_are_not_listed(self, tenancy, other_tenancy, client_for):
        create_flow(workspace=other_tenancy.workspace, name="Rival onboarding")

        body = client_for(tenancy.owner).get(list_url(tenancy)).content.decode()

        assert "Rival onboarding" not in body

    def test_it_groups_by_folder(self, tenancy, client_for):
        create_flow(workspace=tenancy.workspace, name="A", folder="Onboarding")
        create_flow(workspace=tenancy.workspace, name="B")

        body = client_for(tenancy.owner).get(list_url(tenancy)).content.decode()

        assert "Onboarding" in body
        assert "Unfiled" in body

    def test_search_filters_by_name(self, tenancy, client_for):
        create_flow(workspace=tenancy.workspace, name="Welcome series")
        create_flow(workspace=tenancy.workspace, name="Abandoned cart")

        body = client_for(tenancy.owner).get(list_url(tenancy), {"q": "cart"}).content.decode()

        assert "Abandoned cart" in body
        assert "Welcome series" not in body

    def test_archived_flows_are_hidden_until_asked_for(self, tenancy, client_for):
        flow = create_flow(workspace=tenancy.workspace, name="Retired")
        archive_flow(flow)
        client = client_for(tenancy.owner)

        assert "Retired" not in client.get(list_url(tenancy)).content.decode()
        assert "Retired" in client.get(list_url(tenancy), {"status": "archived"}).content.decode()

    def test_an_htmx_request_gets_the_rows_partial(self, tenancy, client_for):
        create_flow(workspace=tenancy.workspace, name="Welcome series")

        response = client_for(tenancy.owner).get(list_url(tenancy), headers={"hx-request": "true"})
        body = response.content.decode()

        assert "Welcome series" in body
        assert "<html" not in body

    def test_the_empty_state_says_which_empty_it_is(self, tenancy, client_for):
        client = client_for(tenancy.owner)

        assert "Create one above" in client.get(list_url(tenancy)).content.decode()
        create_flow(workspace=tenancy.workspace, name="Welcome")
        assert "Nothing matches these filters" in client.get(list_url(tenancy), {"q": "zzz"}).content.decode()


class TestMutations:
    def test_creating_a_flow_makes_a_draft_with_a_first_version(self, tenancy, client_for):
        response = client_for(tenancy.owner).post(
            reverse("flows:create", kwargs={"workspace_id": tenancy.workspace.pk}), {"name": "Welcome"}
        )

        assert response.status_code == 204
        assert "flowsChanged" in response.headers["HX-Trigger"]
        flow = Flow.objects.for_workspace(tenancy.workspace).get()
        assert flow.status == FlowStatus.DRAFT
        assert flow.versions.count() == 1

    def test_creating_without_a_name_is_refused_with_a_toast(self, tenancy, client_for):
        response = client_for(tenancy.owner).post(
            reverse("flows:create", kwargs={"workspace_id": tenancy.workspace.pk}), {"name": "  "}
        )

        assert "Name required" in response.headers["HX-Trigger"]
        assert Flow.objects.for_workspace(tenancy.workspace).count() == 0

    def test_renaming_also_moves_the_folder(self, tenancy, client_for):
        flow = create_flow(workspace=tenancy.workspace, name="Old")

        client_for(tenancy.owner).post(
            action_url("flows:rename", tenancy, flow), {"name": "New", "folder": "Onboarding"}
        )
        flow.refresh_from_db()

        assert (flow.name, flow.folder) == ("New", "Onboarding")

    def test_duplicating_leaves_the_original_alone(self, tenancy, client_for):
        flow = create_flow(workspace=tenancy.workspace, name="Welcome")
        save_draft(flow, graph_for("send_message"), user=tenancy.owner)
        publish(flow, user=tenancy.owner)

        client_for(tenancy.owner).post(action_url("flows:duplicate", tenancy, flow))

        names = sorted(f.name for f in Flow.objects.for_workspace(tenancy.workspace))
        assert names == ["Welcome", "Welcome (copy)"]

    def test_archiving_and_restoring(self, tenancy, client_for):
        flow = create_flow(workspace=tenancy.workspace, name="Welcome")
        client = client_for(tenancy.owner)

        client.post(action_url("flows:archive", tenancy, flow))
        flow.refresh_from_db()
        assert flow.status == FlowStatus.ARCHIVED

        client.post(action_url("flows:restore", tenancy, flow))
        flow.refresh_from_db()
        assert flow.status == FlowStatus.DRAFT


class TestPermissions:
    @pytest.mark.parametrize("role", list(WorkspaceRole))
    def test_every_member_can_read_the_list(self, tenancy, client_for, role):
        assert client_for(tenancy.user_for(role)).get(list_url(tenancy)).status_code == 200

    @pytest.mark.parametrize("role", [WorkspaceRole.AGENT, WorkspaceRole.VIEWER])
    def test_read_only_roles_cannot_mutate(self, tenancy, client_for, role):
        flow = create_flow(workspace=tenancy.workspace, name="Welcome")
        client = client_for(tenancy.user_for(role))

        assert (
            client.post(
                reverse("flows:create", kwargs={"workspace_id": tenancy.workspace.pk}), {"name": "X"}
            ).status_code
            == 403
        )
        assert client.post(action_url("flows:archive", tenancy, flow)).status_code == 403

    def test_the_create_form_is_not_rendered_for_a_viewer(self, tenancy, client_for):
        body = client_for(tenancy.user_for(WorkspaceRole.VIEWER)).get(list_url(tenancy)).content.decode()

        assert "New flow name" not in body

    def test_an_outsider_gets_a_404_not_a_403(self, tenancy, other_tenancy, client_for):
        assert client_for(other_tenancy.owner).get(list_url(tenancy)).status_code == 404


class TestTheBuilderPage:
    def test_it_renders_the_mount_div_with_the_api_urls(self, tenancy, client_for):
        flow = create_flow(workspace=tenancy.workspace, name="Welcome")

        body = client_for(tenancy.owner).get(action_url("flows:edit", tenancy, flow)).content.decode()

        assert 'id="flow-builder"' in body
        assert f'data-flow-id="{flow.pk}"' in body
        assert reverse("flows:api_detail", kwargs={"workspace_id": tenancy.workspace.pk, "flow_id": flow.pk}) in body
        assert reverse("flows:api_schema", kwargs={"workspace_id": tenancy.workspace.pk}) in body

    def test_it_sets_the_csrf_cookie_so_the_first_autosave_has_a_token(self, tenancy, client_for):
        flow = create_flow(workspace=tenancy.workspace, name="Welcome")

        response = client_for(tenancy.owner).get(action_url("flows:edit", tenancy, flow))

        assert "csrftoken" in response.cookies

    def test_a_viewer_is_told_the_canvas_is_read_only(self, tenancy, client_for):
        flow = create_flow(workspace=tenancy.workspace, name="Welcome")

        body = (
            client_for(tenancy.user_for(WorkspaceRole.VIEWER))
            .get(action_url("flows:edit", tenancy, flow))
            .content.decode()
        )

        assert 'data-can-edit="false"' in body
        assert "Read-only" in body

    def test_another_workspaces_flow_is_a_404_here_too(self, tenancy, other_tenancy, client_for):
        victim = create_flow(workspace=tenancy.workspace, name="Victim")
        url = reverse("flows:edit", kwargs={"workspace_id": other_tenancy.workspace.pk, "flow_id": victim.pk})

        assert client_for(other_tenancy.owner).get(url).status_code == 404

    def test_anonymous_visitors_are_sent_to_log_in(self, tenancy):
        flow = create_flow(workspace=tenancy.workspace, name="Welcome")

        assert Client().get(action_url("flows:edit", tenancy, flow)).status_code == 302


class TestNavigation:
    def test_the_sidebar_row_points_at_the_real_list_now(self, tenancy, client_for):
        """The nav registry is data (ground rule 7); this issue swapped one
        entry from the placeholder to flows:list."""
        response = client_for(tenancy.owner).get(list_url(tenancy))
        flows_row = next(
            item for group in response.context["nav_groups"] for item in group["items"] if item["key"] == "flows"
        )

        assert flows_row["url"] == list_url(tenancy)
        assert flows_row["active"] is True

    def test_the_row_stays_lit_on_the_builder_page(self, tenancy, client_for):
        flow = create_flow(workspace=tenancy.workspace, name="Welcome")

        response = client_for(tenancy.owner).get(action_url("flows:edit", tenancy, flow))
        flows_row = next(
            item for group in response.context["nav_groups"] for item in group["items"] if item["key"] == "flows"
        )

        assert flows_row["active"] is True


class TestFolderFilter:
    def test_it_narrows_to_one_folder(self, tenancy, client_for):
        create_flow(workspace=tenancy.workspace, name="Welcome", folder="Onboarding")
        create_flow(workspace=tenancy.workspace, name="Cart", folder="Ecommerce")

        body = client_for(tenancy.owner).get(list_url(tenancy), {"folder": "Onboarding"}).content.decode()

        assert "Welcome" in body
        assert "Cart" not in body

    def test_unfiled_selects_the_flows_with_no_folder(self, tenancy, client_for):
        create_flow(workspace=tenancy.workspace, name="Loose")
        create_flow(workspace=tenancy.workspace, name="Filed", folder="Onboarding")

        body = client_for(tenancy.owner).get(list_url(tenancy), {"folder": "Unfiled"}).content.decode()

        assert "Loose" in body
        assert "Filed" not in body

    def test_the_folder_menu_lists_every_folder_not_just_the_matching_ones(self, tenancy, client_for):
        """Picking a folder must not erase the rest of the menu, or there is no
        way back to the others."""
        create_flow(workspace=tenancy.workspace, name="Welcome", folder="Onboarding")
        create_flow(workspace=tenancy.workspace, name="Cart", folder="Ecommerce")

        response = client_for(tenancy.owner).get(list_url(tenancy), {"folder": "Onboarding"})

        assert response.context["folders"] == ["Ecommerce", "Onboarding"]

    def test_an_unknown_status_filter_is_ignored_rather_than_matched(self, tenancy, client_for):
        create_flow(workspace=tenancy.workspace, name="Welcome")

        body = client_for(tenancy.owner).get(list_url(tenancy), {"status": "'; DROP TABLE"}).content.decode()

        assert "Welcome" in body


class TestRenameValidation:
    def test_a_blank_name_is_refused_and_the_flow_is_untouched(self, tenancy, client_for):
        flow = create_flow(workspace=tenancy.workspace, name="Welcome")

        response = client_for(tenancy.owner).post(action_url("flows:rename", tenancy, flow), {"name": "   "})
        flow.refresh_from_db()

        assert "Name required" in response.headers["HX-Trigger"]
        assert flow.name == "Welcome"

    def test_an_over_long_name_is_truncated_rather_than_erroring(self, tenancy, client_for):
        flow = create_flow(workspace=tenancy.workspace, name="Welcome")

        client_for(tenancy.owner).post(action_url("flows:rename", tenancy, flow), {"name": "x" * 500})
        flow.refresh_from_db()

        assert len(flow.name) == 200
