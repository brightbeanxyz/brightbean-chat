"""The sidebar context processor — one active-state convention (deviation 4)."""

import pytest
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory
from django.urls import ResolverMatch, resolve

from apps.common.context_processors import (
    MAIN_NAV,
    SETTINGS_NAV,
    NavItem,
    navigation_context,
    sidebar_context,
)


def _request(path="/", *, workspace=None, user=None, org_membership=None):
    """A request shaped like one RBACMiddleware has already handled.

    RequestFactory runs no URL resolution and no middleware, and both matter
    here: `resolver_match` is what the active flag is computed from, and
    `request.workspace` is what workspace-scoped rows reverse against.
    """
    request = RequestFactory().get(path)
    request.resolver_match = resolve(path)
    request.workspace = workspace
    request.org_membership = org_membership
    request.user = user if user is not None else AnonymousUser()
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
    @pytest.mark.django_db
    @pytest.mark.parametrize(
        ("suffix", "expected"),
        [
            ("", "dashboard"),
            ("contacts/", "contacts"),
            ("flows/", "flows"),
            ("inbox/", "inbox"),
            ("sequences/", "sequences"),
            ("broadcasts/", "broadcasts"),
        ],
    )
    def test_exactly_one_main_nav_item_is_active_per_route(self, suffix, expected, tenancy):
        """Every main-nav row is workspace-scoped now — issue #31 put the app
        under /w/<uuid>/ (SPEC §16), so the dashboard is the workspace root."""
        path = f"/w/{tenancy.workspace.id}/{suffix}"
        context = navigation_context(_request(path, workspace=tenancy.workspace, user=tenancy.owner))

        active = [i["key"] for g in context["nav_groups"] for i in g["items"] if i["active"]]
        assert active == [expected]

    @pytest.mark.django_db
    @pytest.mark.parametrize(
        ("path", "key", "expected"),
        [
            ("/accounts/settings/", "settings_nav_groups", "profile"),
            ("/accounts/preferences/", "settings_nav_groups", "preferences"),
            ("/organization/settings/", "settings_nav_groups", "org_general"),
            ("/organization/members/", "settings_nav_groups", "org_members"),
            ("/organization/workspaces/", "settings_nav_groups", "org_workspaces"),
            ("WS/settings/", "workspace_settings_nav_groups", "ws_general"),
            ("WS/settings/tags/", "workspace_settings_nav_groups", "ws_tags"),
            ("WS/settings/credentials/", "workspace_settings_nav_groups", "ws_credentials"),
            ("WS/settings/channels/", "workspace_settings_nav_groups", "ws_channels"),
        ],
    )
    def test_exactly_one_settings_item_is_active_per_route(self, path, key, expected, tenancy):
        """Studio needs a separate `settings_active` string, set by 11 different
        views, because its resolver_match checks cannot express this. Here it is
        the same mechanism as the main nav."""
        path = path.replace("WS/", f"/w/{tenancy.workspace.id}/")
        context = navigation_context(_request(path, workspace=tenancy.workspace, user=tenancy.owner))

        active = [i["key"] for g in context[key] for i in g["items"] if i["active"]]
        assert active == [expected]

    @pytest.mark.django_db
    def test_the_two_settings_navs_are_disjoint_views_of_one_structure(self, tenancy):
        context = navigation_context(
            _request(f"/w/{tenancy.workspace.id}/", workspace=tenancy.workspace, user=tenancy.owner)
        )

        account = {i["key"] for g in context["settings_nav_groups"] for i in g["items"]}
        workspace = {i["key"] for g in context["workspace_settings_nav_groups"] for i in g["items"]}

        assert not account & workspace
        assert account | workspace == {i.key for g in SETTINGS_NAV for i in g.items}

    @pytest.mark.django_db
    def test_org_general_and_workspace_general_do_not_collide(self, tenancy):
        """Studio overloads the key "general" between its two settings layouts
        and relies on them never rendering together."""
        org = navigation_context(_request("/organization/settings/", workspace=tenancy.workspace, user=tenancy.owner))
        ws = navigation_context(
            _request(f"/w/{tenancy.workspace.id}/settings/", workspace=tenancy.workspace, user=tenancy.owner)
        )

        def active(ctx, key):
            return [i["key"] for g in ctx[key] for i in g["items"] if i["active"]]

        assert active(org, "settings_nav_groups") == ["org_general"]
        assert active(ws, "workspace_settings_nav_groups") == ["ws_general"]

    def test_a_generic_route_name_in_another_namespace_does_not_light_a_row(self):
        """Matching is on the namespaced view_name, not the bare url_name.

        Studio compares url_name and had to hand-write a compound
        `url_name == "list" and app_name == "notifications"` guard at the one
        row where that had already collided. Layer 2 onwards adds namespaced
        apps full of generic route names like `list` and `detail`, so this is
        the difference between one convention holding and needing that patch
        again at every call site.
        """
        item = NavItem(key="contacts", label="Contacts", icon="contacts", url_name="contacts")
        request = RequestFactory().get("/flows/")
        request.resolver_match = ResolverMatch(
            func=lambda r: None, args=(), kwargs={}, url_name="contacts", namespaces=["flows"]
        )

        # view_name is "flows:contacts" — a different section entirely.
        assert item.resolved(request, {})["active"] is False

    @pytest.mark.django_db
    def test_an_item_can_cover_several_routes(self, tenancy):
        item = NavItem(
            key="contacts",
            label="Contacts",
            icon="contacts",
            url_name="contacts",
            url_names=frozenset({"contacts", "settings_ws_fields"}),
            workspace_scoped=True,
        )
        request = _request(f"/w/{tenancy.workspace.id}/settings/fields/", workspace=tenancy.workspace)

        assert item.resolved(request, {}, tenancy.workspace.id)["active"] is True

    def test_a_route_outside_the_nav_lights_nothing_up(self):
        context = navigation_context(_request("/ui/"))

        assert not [i for g in context["nav_groups"] for i in g["items"] if i["active"]]

    def test_a_request_with_no_resolver_match_does_not_raise(self):
        """Error pages render without one."""
        context = navigation_context(RequestFactory().get("/"))

        assert not [i for g in context["nav_groups"] for i in g["items"] if i["active"]]


class TestReverseCache:
    def test_a_resolvable_name_is_cached_and_stable(self):
        from apps.common.context_processors import reverse_cached

        assert reverse_cached("accounts:settings") == "/accounts/settings/"
        assert reverse_cached("accounts:settings") == "/accounts/settings/"

    @pytest.mark.django_db
    def test_workspace_scoped_urls_are_keyed_by_workspace(self, tenancy, other_tenancy):
        """The id is part of the cache key, or switching workspace would keep
        serving the previous workspace's URLs."""
        from apps.common.context_processors import reverse_cached

        first = reverse_cached("workspaces:dashboard", workspace_id=tenancy.workspace.id)
        second = reverse_cached("workspaces:dashboard", workspace_id=other_tenancy.workspace.id)

        assert first != second
        assert str(tenancy.workspace.id) in first
        assert str(other_tenancy.workspace.id) in second

    def test_an_unresolvable_name_is_none_and_is_not_retried_into_an_exception(self):
        """Caching the miss is the point: without it every request would build
        a NoReverseMatch — Django describes the whole failed lookup in the
        message — just to throw it away."""
        from apps.common.context_processors import _URL_CACHE, reverse_cached

        assert reverse_cached("no_such_route_anywhere") is None
        assert any(k[1] == "no_such_route_anywhere" for k in _URL_CACHE)

    def test_the_cache_is_dropped_when_the_urlconf_changes(self):
        """Otherwise one test's routes leak into the next through a module-level
        dict — override_settings(ROOT_URLCONF=...) is common in Django suites."""
        from django.test import override_settings

        from apps.common.context_processors import _URL_CACHE, reverse_cached

        reverse_cached("dashboard")
        assert _URL_CACHE

        with override_settings(ROOT_URLCONF="tests.testapp.urls_does_not_exist"):
            assert _URL_CACHE == {}

        assert _URL_CACHE == {}


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

    @pytest.mark.django_db
    def test_badges_default_to_zero_and_render_nothing(self, tenancy):
        context = navigation_context(
            _request(f"/w/{tenancy.workspace.id}/", workspace=tenancy.workspace, user=tenancy.owner)
        )

        inbox = next(i for g in context["nav_groups"] for i in g["items"] if i["key"] == "inbox")
        assert inbox["badge"] == 0


class TestTenancyIntegration:
    """What issue #31 now supplies, which the shell used to stub out.

    These keys were `# TODO(L1-A)` placeholders — an empty list and a False —
    with a template contract this module invented. They read real membership
    data now, so the switcher and the create control mean something.
    """

    @pytest.mark.django_db
    def test_the_switcher_lists_the_workspaces_the_user_can_reach(self, tenancy, other_tenancy):
        """Membership is the authority, not org contents: a user must not see a
        neighbouring tenant's workspace in their own switcher."""
        context = navigation_context(
            _request(f"/w/{tenancy.workspace.id}/", workspace=tenancy.workspace, user=tenancy.owner)
        )

        names = [w["name"] for w in context["sidebar_workspaces"]]
        assert tenancy.workspace.name in names
        assert other_tenancy.workspace.name not in names

    @pytest.mark.django_db
    def test_every_switcher_entry_has_a_real_href(self, tenancy):
        context = navigation_context(
            _request(f"/w/{tenancy.workspace.id}/", workspace=tenancy.workspace, user=tenancy.owner)
        )

        for entry in context["sidebar_workspaces"]:
            assert entry["url"].startswith(f"/w/{tenancy.workspace.id}")

    @pytest.mark.django_db
    def test_the_current_workspace_is_marked(self, tenancy):
        context = navigation_context(
            _request(f"/w/{tenancy.workspace.id}/", workspace=tenancy.workspace, user=tenancy.owner)
        )

        assert [w["is_current"] for w in context["sidebar_workspaces"]] == [True]

    @pytest.mark.django_db
    def test_archived_workspaces_are_not_offered(self, tenancy):
        """Nobody can be sent to an archived workspace, so it is not a
        destination the switcher should hold."""
        tenancy.workspace.is_archived = True
        tenancy.workspace.save(update_fields=["is_archived"])

        context = navigation_context(_request("/organization/settings/", user=tenancy.owner))

        assert context["sidebar_workspaces"] == []

    @pytest.mark.django_db
    def test_creating_a_workspace_is_an_org_tier_action(self, tenancy):
        """Hidden rather than rendered and refused."""
        owner_ctx = navigation_context(
            _request("/organization/settings/", user=tenancy.owner, org_membership=_org_membership(tenancy.owner))
        )
        viewer = tenancy.user_for("viewer")
        viewer_ctx = navigation_context(
            _request("/organization/settings/", user=viewer, org_membership=_org_membership(viewer))
        )

        assert owner_ctx["can_create_workspace"] is True
        assert viewer_ctx["can_create_workspace"] is False

    @pytest.mark.django_db
    def test_the_logout_control_is_wired_now_that_allauth_is_installed(self, tenancy):
        context = navigation_context(_request("/organization/settings/", user=tenancy.owner))

        assert context["logout_url"] == "/accounts/logout/"

    @pytest.mark.django_db
    def test_workspace_scoped_rows_vanish_without_a_workspace(self, tenancy):
        """RBACMiddleware leaves request.workspace None when every workspace is
        archived. A row pointing into a workspace that is not there is worse
        than no row."""
        context = navigation_context(_request("/organization/settings/", user=tenancy.owner))

        assert context["nav_groups"] == []
        assert context["workspace_settings_nav_groups"] == []

    def test_channel_connections_is_still_a_placeholder(self):
        """Issue #4 owns ChannelConnection; #31's credential store is
        per-platform configuration, not a connected account."""
        assert navigation_context(_request())["channel_connections"] == []

    def test_show_app_shell_is_set(self):
        assert navigation_context(_request())["show_app_shell"] is True


def _org_membership(user):
    from apps.members.models import OrgMembership

    return OrgMembership.objects.filter(user=user).first()
