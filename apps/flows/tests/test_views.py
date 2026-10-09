"""The flow list and the builder's host page."""

from unittest import mock

import pytest
from django.test import Client
from django.urls import reverse

from apps.flows.fixtures import graph_for
from apps.flows.models import Flow, FlowStatus
from apps.flows.portability.library import STARTER_CATEGORY
from apps.flows.services import archive_flow, create_flow, publish, save_draft
from apps.flows.views import UNFILED_VALUE
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

    def test_it_groups_by_status_and_names_the_folder_on_the_row(self, tenancy, client_for):
        """Sections answer "what is running" (HANDOFF §3); the folder is a
        filter and a word on the row rather than the grouping."""
        live = create_flow(workspace=tenancy.workspace, name="Running one", folder="Onboarding")
        save_draft(live, graph_for("send_message"), user=tenancy.owner)
        publish(live, user=tenancy.owner)
        create_flow(workspace=tenancy.workspace, name="Unfinished one")

        response = client_for(tenancy.owner).get(list_url(tenancy))
        groups = response.context["groups"]

        assert [group["label"] for group in groups] == ["Live", "Draft"]
        assert [[f.name for f in group["flows"]] for group in groups] == [["Running one"], ["Unfinished one"]]
        assert "Onboarding ·" in response.content.decode()

    def test_the_summary_counts_the_whole_workspace_not_the_filtered_view(self, tenancy, client_for):
        create_flow(workspace=tenancy.workspace, name="One")
        create_flow(workspace=tenancy.workspace, name="Two")

        response = client_for(tenancy.owner).get(list_url(tenancy), {"q": "One"})

        draft = next(card for card in response.context["summary"] if card["status"] == FlowStatus.DRAFT)
        assert draft["count"] == 2

    def test_an_htmx_refresh_carries_the_summary_out_of_band(self, tenancy, client_for):
        """The cards sit above the toolbar, outside the swapped rows, so a
        switch that moves a flow between sections must refresh them too."""
        create_flow(workspace=tenancy.workspace, name="One")

        body = client_for(tenancy.owner).get(list_url(tenancy), headers={"hx-request": "true"}).content.decode()

        assert 'id="flow-summary"' in body
        assert 'hx-swap-oob="true"' in body

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

    def test_an_offline_flow_says_so_and_has_its_own_filter(self, tenancy, client_for):
        """Listed by default — unlike an archived flow it is still being worked
        on — and labelled Offline rather than passed off as a draft."""
        from apps.flows.services import take_offline

        flow = create_flow(workspace=tenancy.workspace, name="Paused welcome")
        save_draft(flow, graph_for("send_message"), user=tenancy.owner)
        publish(flow, user=tenancy.owner)
        take_offline(flow)
        create_flow(workspace=tenancy.workspace, name="Unfinished draft")
        client = client_for(tenancy.owner)

        everything = client.get(list_url(tenancy)).content.decode()
        offline_only = client.get(list_url(tenancy), {"status": "offline"}).content.decode()

        assert "Paused welcome" in everything
        assert "status-pill-offline" in everything
        assert "Paused welcome" in offline_only
        assert "Unfinished draft" not in offline_only

    def test_an_htmx_request_gets_the_rows_partial(self, tenancy, client_for):
        create_flow(workspace=tenancy.workspace, name="Welcome series")

        response = client_for(tenancy.owner).get(list_url(tenancy), headers={"hx-request": "true"})
        body = response.content.decode()

        assert "Welcome series" in body
        assert "<html" not in body

    def test_the_empty_state_says_which_empty_it_is(self, tenancy, client_for):
        client = client_for(tenancy.owner)

        # The empty state's own wording, not the page lede: the lede renders
        # for a full workspace too, so asserting it would pass with no empty
        # state on the page at all.
        assert "No flows here yet" in client.get(list_url(tenancy)).content.decode()
        create_flow(workspace=tenancy.workspace, name="Welcome")
        assert "Nothing matches these filters" in client.get(list_url(tenancy), {"q": "zzz"}).content.decode()


class TestTemplatesInTheEmptyState:
    """The gallery, where somebody with nothing actually lands.

    The full gallery is a button in the toolbar, which is one click and one
    guess away from a person who does not yet know the app ships templates at
    all. These cards are the same library, on the screen that would otherwise
    offer a naked text field.
    """

    def test_an_empty_workspace_is_offered_templates(self, tenancy, client_for):
        response = client_for(tenancy.owner).get(list_url(tenancy))

        assert response.context["template_cards"], "an empty workspace got no cards"
        assert "Start from a template" in response.content.decode()

    def test_it_links_to_the_full_gallery(self, tenancy, client_for):
        response = client_for(tenancy.owner).get(list_url(tenancy))
        total = response.context["template_total"]

        assert total > len(response.context["template_cards"])
        assert f"Browse all {total}" in response.content.decode()

    def test_a_workspace_with_a_flow_is_not(self, tenancy, client_for):
        create_flow(workspace=tenancy.workspace, name="Welcome")

        response = client_for(tenancy.owner).get(list_url(tenancy))

        assert response.context["template_cards"] == []
        assert "Browse all" not in response.content.decode()

    def test_a_filtered_empty_result_keeps_the_filter_wording(self, tenancy, client_for):
        """An empty *result* is not an empty workspace. Offering templates to
        somebody whose filter matched nothing answers a question they did not
        ask and hides the one they did."""
        create_flow(workspace=tenancy.workspace, name="Welcome")

        response = client_for(tenancy.owner).get(list_url(tenancy), {"q": "zzz"})

        assert response.context["template_cards"] == []
        assert "Nothing matches these filters" in response.content.decode()

    @pytest.mark.parametrize("role", [WorkspaceRole.AGENT, WorkspaceRole.VIEWER])
    def test_a_role_that_cannot_edit_is_not_offered_them(self, tenancy, client_for, role):
        response = client_for(tenancy.user_for(role)).get(list_url(tenancy))

        assert response.context["template_cards"] == []

    def test_the_htmx_refresh_answers_with_rows_alone(self, tenancy, client_for):
        """The panel sits in the page, above #flow-rows, not inside the partial
        the filters re-fetch. So an HTMX refresh answers with rows and nothing
        else — and the consequence, which is the reason to write it down: the
        panel stays on screen until the next full load, including just after
        the Create that made it wrong."""
        response = client_for(tenancy.owner).get(list_url(tenancy), headers={"hx-request": "true"})
        body = response.content.decode()

        # "Browse all", not the heading: the rows partial's own empty state
        # points *at* the panel ("Start from a template above"), so the heading
        # appears either way. The link only exists inside the panel.
        assert "Browse all" not in body
        assert "<html" not in body

    def test_the_cards_say_which_channel_they_need(self, tenancy, client_for):
        """The badge is the reason the empty state is worth more than a list of
        names: "Needs Instagram" is answerable before you click. Named in full
        rather than as the bare word, which "Also needs:" would also satisfy."""
        body = client_for(tenancy.owner).get(list_url(tenancy)).content.decode()

        assert "Needs Instagram" in body

    def test_the_featured_cards_are_not_all_one_channel(self, tenancy, client_for):
        """What _featured() exists for. The library is alphabetical, so a plain
        slice is four near-identical instagram-comment-* cards — the same channel
        four times over, from the one screen meant to suggest the range."""
        response = client_for(tenancy.owner).get(list_url(tenancy))

        cards = response.context["template_cards"]
        assert [entry["card"].category for entry in cards][:3] == [STARTER_CATEGORY] * 3
        platforms = {platform["key"] for entry in cards for platform in entry["platforms"]}
        assert len(platforms) > 1, f"every featured card is on the same channel: {platforms}"

    def test_the_cards_here_carry_no_alpine_filter(self, tenancy, client_for):
        """matches() is defined in template_gallery.html's x-data. This panel
        sits inside list.html's, which has no such method, so an unguarded
        x-show would throw inside Alpine's evaluator for every card on every
        render of the core Flows page — silent apart from the console."""
        body = client_for(tenancy.owner).get(list_url(tenancy)).content.decode()

        assert "Start from a template" in body, "the panel did not render, so this asserts nothing"
        assert "matches(" not in body

    def test_a_populated_list_asks_the_library_for_nothing(self, tenancy, client_for, monkeypatch):
        """The guard, asserted rather than assumed: this path runs on every HTMX
        refresh of every workspace's flow list."""
        import apps.flows.views as views

        def explode():
            raise AssertionError("the template library was read for a populated list")

        monkeypatch.setattr(views, "shipped_templates", explode)
        create_flow(workspace=tenancy.workspace, name="Welcome")

        assert client_for(tenancy.owner).get(list_url(tenancy)).status_code == 200


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

    def test_restoring_past_the_plan_limit_is_a_toast_not_a_500(self, tenancy, client_for, monkeypatch):
        """Restoring a flow with a published version puts it back live, so it
        takes the plan check — and its refusal used to escape as a 500."""
        import json

        from apps.flows import services

        flow = create_flow(workspace=tenancy.workspace, name="Welcome")
        save_draft(flow, graph_for("send_message"), user=tenancy.owner)
        publish(flow, user=tenancy.owner)
        archive_flow(flow)

        def refuse(flow):
            raise services.FlowPlanLimitError("Your plan allows 1 active automation; switch one off to free up a slot.")

        monkeypatch.setattr(services, "_check_plan_allows_activation", refuse)

        response = client_for(tenancy.owner).post(action_url("flows:restore", tenancy, flow))

        toast = json.loads(response.headers["HX-Trigger"])["showToast"]
        assert response.status_code == 204
        assert toast["title"] == "Not restored"
        assert "switch one off" in toast["body"]
        flow.refresh_from_db()
        assert flow.status == FlowStatus.ARCHIVED


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
        offline = reverse("flows:api_offline", kwargs={"workspace_id": tenancy.workspace.pk, "flow_id": flow.pk})
        assert f'data-offline-url="{offline}"' in body

    def test_it_sets_the_csrf_cookie_so_the_first_autosave_has_a_token(self, tenancy, client_for):
        flow = create_flow(workspace=tenancy.workspace, name="Welcome")

        response = client_for(tenancy.owner).get(action_url("flows:edit", tenancy, flow))

        assert "csrftoken" in response.cookies

    @pytest.mark.parametrize("built", [True, False], ids=["bundle built", "bundle missing"])
    def test_a_viewer_is_told_the_canvas_is_read_only(self, tenancy, client_for, built):
        """Said in both of the page's states, because CI runs without the
        builder bundle and a developer usually has one."""
        flow = create_flow(workspace=tenancy.workspace, name="Welcome")

        with mock.patch(
            "apps.flows.templatetags.flow_builder.finders.find",
            side_effect=(lambda path: f"/somewhere/{path}") if built else (lambda path: None),
        ):
            body = (
                client_for(tenancy.user_for(WorkspaceRole.VIEWER))
                .get(action_url("flows:edit", tenancy, flow))
                .content.decode()
            )

        assert 'data-can-edit="false"' in body
        assert "Read-only" in body

    def test_the_header_leaves_the_version_to_the_builder(self, tenancy, client_for):
        """The page header says nothing about draft-versus-live.

        It is rendered once and cannot re-render, so a version printed here
        survived a publish unchanged — and survived a reload unchanged, because
        the server had already moved on and the header had not. The island reads
        both from the flow API instead. Named for the intent, so the negative
        assertion does not read as arbitrary.
        """
        flow = create_flow(workspace=tenancy.workspace, name="Welcome")

        body = client_for(tenancy.owner).get(action_url("flows:edit", tenancy, flow)).content.decode()

        assert "Draft v" not in body
        assert 'id="flow-builder"' in body

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

        body = client_for(tenancy.owner).get(list_url(tenancy), {"folder": UNFILED_VALUE}).content.decode()

        assert "Loose" in body
        assert "Filed" not in body

    def test_the_folder_menu_lists_every_folder_not_just_the_matching_ones(self, tenancy, client_for):
        """Picking a folder must not erase the rest of the menu, or there is no
        way back to the others."""
        create_flow(workspace=tenancy.workspace, name="Welcome", folder="Onboarding")
        create_flow(workspace=tenancy.workspace, name="Cart", folder="Ecommerce")

        response = client_for(tenancy.owner).get(list_url(tenancy), {"folder": "Onboarding"})

        folder_group = next(g for g in response.context["filter_groups"] if g["key"] == "folder")
        assert folder_group["options"] == [
            (UNFILED_VALUE, "Unfiled"),
            ("Ecommerce", "Ecommerce"),
            ("Onboarding", "Onboarding"),
        ]

    def test_an_unknown_status_filter_falls_back_to_the_default_view(self, tenancy, client_for):
        """Not merely "ignored": an if/elif here let an unrecognised value skip
        the archived exclusion too, so `?status=bogus` listed archived flows
        among the live ones."""
        create_flow(workspace=tenancy.workspace, name="Welcome")
        archive_flow(create_flow(workspace=tenancy.workspace, name="Retired"))

        body = client_for(tenancy.owner).get(list_url(tenancy), {"status": "'; DROP TABLE"}).content.decode()

        assert "Welcome" in body
        assert "Retired" not in body

    def test_several_folders_and_unfiled_combine(self, tenancy, client_for):
        """The popover lets a reader pick more than one value in a group, and
        they arrive as a repeated parameter — all of them must count."""
        create_flow(workspace=tenancy.workspace, name="Welcome", folder="Onboarding")
        create_flow(workspace=tenancy.workspace, name="Cart", folder="Ecommerce")
        create_flow(workspace=tenancy.workspace, name="Loose")
        create_flow(workspace=tenancy.workspace, name="Survey", folder="Research")

        body = (
            client_for(tenancy.owner)
            .get(list_url(tenancy), {"folder": ["Onboarding", "Ecommerce", UNFILED_VALUE]})
            .content.decode()
        )

        assert "Welcome" in body
        assert "Cart" in body
        assert "Loose" in body
        assert "Survey" not in body

    def test_several_statuses_combine(self, tenancy, client_for):
        from apps.flows.services import take_offline

        live = create_flow(workspace=tenancy.workspace, name="Running")
        save_draft(live, graph_for("send_message"), user=tenancy.owner)
        publish(live, user=tenancy.owner)
        paused = create_flow(workspace=tenancy.workspace, name="Paused")
        save_draft(paused, graph_for("send_message"), user=tenancy.owner)
        publish(paused, user=tenancy.owner)
        take_offline(paused)
        create_flow(workspace=tenancy.workspace, name="Unfinished")

        response = client_for(tenancy.owner).get(list_url(tenancy), {"status": ["active", "offline", "bogus"]})
        body = response.content.decode()

        assert "Running" in body
        assert "Paused" in body
        assert "Unfinished" not in body
        assert response.context["filters"]["status"] == ["active", "offline"]

    def test_a_folder_named_unfiled_is_still_reachable(self, tenancy, client_for):
        """The filter value is a sentinel, not the label, so a real folder of
        that name is not shadowed by the "no folder" pseudo-group."""
        create_flow(workspace=tenancy.workspace, name="Filed there", folder="Unfiled")
        create_flow(workspace=tenancy.workspace, name="Genuinely loose")
        client = client_for(tenancy.owner)

        by_name = client.get(list_url(tenancy), {"folder": "Unfiled"}).content.decode()
        assert "Filed there" in by_name
        assert "Genuinely loose" not in by_name

        by_sentinel = client.get(list_url(tenancy), {"folder": UNFILED_VALUE}).content.decode()
        assert "Genuinely loose" in by_sentinel
        assert "Filed there" not in by_sentinel


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


def _live_flow(tenancy, name="Live one"):
    flow = create_flow(workspace=tenancy.workspace, name=name)
    save_draft(flow, graph_for("send_message"), user=tenancy.owner)
    publish(flow, user=tenancy.owner)
    return flow


class TestTheLiveSwitch:
    """The flow list's switch (views.flow_set_live)."""

    def test_off_takes_a_live_flow_offline(self, tenancy, client_for):
        flow = _live_flow(tenancy)

        response = client_for(tenancy.owner).post(action_url("flows:set_live", tenancy, flow), {"live": "0"})

        flow.refresh_from_db()
        assert flow.status == FlowStatus.OFFLINE
        assert "flowsChanged" in response.headers["HX-Trigger"]

    def test_on_sets_an_unchanged_offline_flow_live_again(self, tenancy, client_for):
        from apps.flows.services import take_offline

        flow = _live_flow(tenancy)
        take_offline(flow)

        client_for(tenancy.owner).post(action_url("flows:set_live", tenancy, flow), {"live": "1"})

        flow.refresh_from_db()
        assert flow.status == FlowStatus.ACTIVE

    def test_on_refuses_a_flow_edited_since_it_went_offline(self, tenancy, client_for):
        """publish() sets the newest version live, and a list switch is no
        place to publish edits nobody has reviewed."""
        from apps.flows.services import take_offline

        flow = _live_flow(tenancy)
        take_offline(flow)
        save_draft(flow, graph_for("send_message"), user=tenancy.owner)
        client = client_for(tenancy.owner)

        response = client.post(action_url("flows:set_live", tenancy, flow), {"live": "1"})
        row = next(f for g in client.get(list_url(tenancy)).context["groups"] for f in g["flows"])

        flow.refresh_from_db()
        assert flow.status == FlowStatus.OFFLINE
        assert "have not been live yet" in response.headers["HX-Trigger"]
        assert row.can_switch is False

    def test_on_refuses_a_draft(self, tenancy, client_for):
        flow = create_flow(workspace=tenancy.workspace, name="Never ran")

        client_for(tenancy.owner).post(action_url("flows:set_live", tenancy, flow), {"live": "1"})

        flow.refresh_from_db()
        assert flow.status == FlowStatus.DRAFT

    def test_off_on_a_flow_that_is_not_live_says_so(self, tenancy, client_for):
        flow = create_flow(workspace=tenancy.workspace, name="Never ran")

        response = client_for(tenancy.owner).post(action_url("flows:set_live", tenancy, flow), {"live": "0"})

        assert response.status_code == 204
        assert "Not set offline" in response.headers["HX-Trigger"]

    @pytest.mark.parametrize("role", [WorkspaceRole.AGENT, WorkspaceRole.VIEWER])
    def test_a_role_that_cannot_edit_cannot_flip_it(self, tenancy, client_for, role):
        flow = _live_flow(tenancy)

        response = client_for(tenancy.user_for(role)).post(action_url("flows:set_live", tenancy, flow), {"live": "0"})

        flow.refresh_from_db()
        assert response.status_code == 403
        assert flow.status == FlowStatus.ACTIVE

    def test_the_row_counts_runs_from_the_last_seven_days(self, tenancy, client_for):
        flow = _live_flow(tenancy)

        row = next(f for g in client_for(tenancy.owner).get(list_url(tenancy)).context["groups"] for f in g["flows"])

        assert row.pk == flow.pk
        assert row.runs_recent == 0
        assert row.can_switch is True
