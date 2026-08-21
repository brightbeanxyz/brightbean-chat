"""The sidebar context processor — one active-state convention (deviation 4)."""

import pytest
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory
from django.urls import resolve

from apps.common.context_processors import MAIN_NAV, SETTINGS_NAV, navigation_context, sidebar_context


def _request(path="/dashboard/"):
    """A request carrying the resolver_match a real one would have.

    RequestFactory does not run URL resolution, and resolver_match is exactly
    what the active flag is computed from.
    """
    request = RequestFactory().get(path)
    request.resolver_match = resolve(path)
    return request


class TestAnonymousShortCircuit:
    def test_anonymous_users_get_nothing_at_all(self):
        """Not empty defaults — nothing. Every {% if %} in the shell then falls
        through, and the landing and auth pages cost zero queries."""
        request = _request()
        request.user = AnonymousUser()

        assert sidebar_context(request) == {}

    def test_a_request_without_a_user_attribute_does_not_raise(self):
        """Covers requests that never saw AuthenticationMiddleware: a bare
        RequestFactory request in a test, or a template rendered from a
        management command."""
        assert sidebar_context(RequestFactory().get("/")) == {}

    @pytest.mark.django_db
    def test_the_anonymous_path_runs_no_queries(self, django_assert_num_queries):
        request = _request()
        request.user = AnonymousUser()

        with django_assert_num_queries(0):
            sidebar_context(request)


class TestActiveFlag:
    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("/dashboard/", "dashboard"),
            ("/contacts/", "contacts"),
            ("/flows/", "flows"),
            ("/inbox/", "inbox"),
            ("/sequences/", "sequences"),
            ("/broadcasts/", "broadcasts"),
        ],
    )
    def test_exactly_one_main_nav_item_is_active_per_route(self, path, expected):
        context = navigation_context(_request(path))

        active = [i["key"] for g in context["nav_groups"] for i in g["items"] if i["active"]]
        assert active == [expected]

    @pytest.mark.parametrize(
        ("path", "key", "expected"),
        [
            ("/settings/profile/", "settings_nav_groups", "profile"),
            ("/settings/preferences/", "settings_nav_groups", "preferences"),
            ("/settings/organization/", "settings_nav_groups", "org_general"),
            ("/settings/organization/members/", "settings_nav_groups", "org_members"),
            ("/settings/workspace/", "workspace_settings_nav_groups", "ws_general"),
            ("/settings/workspace/tags/", "workspace_settings_nav_groups", "ws_tags"),
        ],
    )
    def test_exactly_one_settings_item_is_active_per_route(self, path, key, expected):
        """Studio needs a separate `settings_active` string, set by 11 different
        views, because its resolver_match checks cannot express this. Here it is
        the same mechanism as the main nav."""
        context = navigation_context(_request(path))

        active = [i["key"] for g in context[key] for i in g["items"] if i["active"]]
        assert active == [expected]

    def test_the_two_settings_navs_are_disjoint_views_of_one_structure(self):
        context = navigation_context(_request())

        account = {i["key"] for g in context["settings_nav_groups"] for i in g["items"]}
        workspace = {i["key"] for g in context["workspace_settings_nav_groups"] for i in g["items"]}

        assert not account & workspace
        assert account | workspace == {i.key for g in SETTINGS_NAV for i in g.items}

    def test_org_general_and_workspace_general_do_not_collide(self):
        """Studio overloads the key "general" between its two settings layouts
        and relies on them never rendering together."""
        org = navigation_context(_request("/settings/organization/"))
        ws = navigation_context(_request("/settings/workspace/"))

        def active(ctx, key):
            return [i["key"] for g in ctx[key] for i in g["items"] if i["active"]]

        assert active(org, "settings_nav_groups") == ["org_general"]
        assert active(ws, "workspace_settings_nav_groups") == ["ws_general"]

    def test_a_route_outside_the_nav_lights_nothing_up(self):
        context = navigation_context(_request("/ui/"))

        assert not [i for g in context["nav_groups"] for i in g["items"] if i["active"]]

    def test_a_request_with_no_resolver_match_does_not_raise(self):
        """Error pages render without one."""
        context = navigation_context(RequestFactory().get("/"))

        assert not [i for g in context["nav_groups"] for i in g["items"] if i["active"]]


class TestNavStructure:
    def test_every_nav_target_resolves_to_a_real_url(self):
        """A nav entry pointing at a name nothing registers renders "#", which
        is silent. Catch it here instead."""
        context = navigation_context(_request())

        all_groups = context["nav_groups"] + context["settings_nav_groups"] + context["workspace_settings_nav_groups"]
        for group in all_groups:
            for item in group["items"]:
                assert item["url"] != "#", f"{item['key']} does not resolve"
                assert item["url"].startswith("/")

    def test_nav_item_keys_are_unique_across_both_navs(self):
        keys = [i.key for g in MAIN_NAV + SETTINGS_NAV for i in g.items]

        assert len(keys) == len(set(keys))

    def test_the_product_nav_is_the_one_the_issue_specifies(self):
        keys = [i.key for g in MAIN_NAV for i in g.items]

        assert set(keys) == {"dashboard", "contacts", "flows", "inbox", "sequences", "broadcasts"}

    def test_settings_groups_match_the_brief(self):
        assert [g.label for g in SETTINGS_NAV] == ["Account", "Organization", "Workspace"]

    def test_badges_default_to_zero_and_render_nothing(self):
        context = navigation_context(_request())

        inbox = next(i for g in context["nav_groups"] for i in g["items"] if i["key"] == "inbox")
        assert inbox["badge"] == 0


class TestPendingWorkstreamPlaceholders:
    """Keys #31/#4/#14 will fill in. They must exist and be empty, not absent —
    the shell reads them on every request."""

    def test_placeholders_are_present_and_empty(self):
        context = navigation_context(_request())

        assert context["sidebar_workspaces"] == []
        assert context["channel_connections"] == []
        assert context["current_workspace"] is None
        assert context["can_create_workspace"] is False

    def test_logout_url_is_none_until_allauth_lands(self):
        """The footer omits the control rather than 500-ing on {% url %}."""
        assert navigation_context(_request())["logout_url"] is None

    def test_show_app_shell_is_set(self):
        assert navigation_context(_request())["show_app_shell"] is True
