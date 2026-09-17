"""The sidebar context processor — one active-state convention (deviation 4)."""

from pathlib import Path

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
from apps.members.roles import WorkspaceRole


def _request(path="/", *, workspace=None, user=None, org_membership=None, workspace_membership=None):
    """A request shaped like one RBACMiddleware has already handled.

    RequestFactory runs no URL resolution and no middleware, and three things
    matter here: `resolver_match` is what the active flag is computed from,
    `request.workspace` is what workspace-scoped rows reverse against, and
    `request.workspace_membership` is what the per-row permission check reads.

    The membership is looked up from the workspace and user when it is not
    passed, because that is what the middleware does — including on routes with
    no workspace in the URL, where it falls back to the user's last workspace
    (see RBACMiddleware.__call__). Leaving it None here would hide every
    permission-gated row and make these tests pass for the wrong reason.
    """
    request = RequestFactory().get(path)
    request.resolver_match = resolve(path)
    request.workspace = workspace
    request.org_membership = org_membership
    request.user = user if user is not None else AnonymousUser()
    if workspace_membership is None and workspace is not None and user is not None:
        from apps.members.models import WorkspaceMembership

        workspace_membership = WorkspaceMembership.objects.filter(user=user, workspace=workspace).first()
    request.workspace_membership = workspace_membership
    if org_membership is None and user is not None and not isinstance(user, AnonymousUser):
        # The middleware resolves this for every authenticated request, and the
        # organisation rows are gated on it.
        from apps.members.models import OrgMembership

        request.org_membership = OrgMembership.objects.filter(user=user).order_by("created_at").first()
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
            # Sequences is a tab on the flows page now, not a rail row of its
            # own, so it lights the row its tab strip hangs under.
            ("sequences/", "flows"),
            ("broadcasts/", "broadcasts"),
            ("media/", "media"),
        ],
    )
    def test_exactly_one_main_nav_item_is_active_per_route(self, suffix, expected, tenancy):
        """Every main-nav row is workspace-scoped now — issue #31 put the app
        under /w/<uuid>/ (SPEC §16), so the dashboard is the workspace root.

        Both halves of the rail are searched: Library and Settings sit in the
        bottom group, and a route lighting a row there is still exactly one row.
        """
        path = f"/w/{tenancy.workspace.id}/{suffix}"
        context = navigation_context(_request(path, workspace=tenancy.workspace, user=tenancy.owner))

        rail = context["nav_groups"] + context["nav_footer_groups"]
        active = [i["key"] for g in rail for i in g["items"] if i["active"]]
        assert active == [expected]

    @pytest.mark.django_db
    @pytest.mark.parametrize(
        ("path", "key", "expected"),
        [
            ("/accounts/settings/", "settings_nav_groups", "profile"),
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
    def test_there_is_one_settings_nav_filtered_per_viewer(self, tenancy):
        """Both layouts render the same list now, gated row by row.

        There used to be two group lists, so that an Editor on workspace
        settings would not be shown organisation rows. The intent was right and
        the mechanism was too coarse in both directions: it hid every Workspace
        row from the account settings page — where the rail's Settings row lands
        — leaving Channels, Tags, Labels and five more with no entry point at
        all, and inside a group it filtered nothing, so the Editor still saw
        rows they would be refused at.
        """
        context = navigation_context(
            _request(f"/w/{tenancy.workspace.id}/", workspace=tenancy.workspace, user=tenancy.owner)
        )

        account = [i["key"] for g in context["settings_nav_groups"] for i in g["items"]]
        workspace = [i["key"] for g in context["workspace_settings_nav_groups"] for i in g["items"]]

        assert account == workspace
        # An owner holds every permission, so they see the whole structure.
        assert set(account) == {i.key for g in SETTINGS_NAV for i in g.items}

    @pytest.mark.django_db
    def test_a_row_is_hidden_from_a_viewer_its_page_would_refuse(self, tenancy):
        """The reason one list is safe to render.

        An Agent holds `reply_in_inbox` but not `manage_channels`, so Labels is
        theirs and Channels is not. A link that always answers 403 reads as a
        bug rather than as a boundary.
        """
        agent = tenancy.user_for(WorkspaceRole.AGENT)
        context = navigation_context(_request(f"/w/{tenancy.workspace.id}/", workspace=tenancy.workspace, user=agent))
        keys = {i["key"] for g in context["settings_nav_groups"] for i in g["items"]}

        assert "ws_labels" in keys
        assert "ws_channels" not in keys
        assert "ws_general" not in keys
        # And the rows nobody is gated out of are still there.
        assert "profile" in keys

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
            url_name="contacts:list",
            url_names=frozenset({"contacts:list", "contacts:field_list"}),
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

    @pytest.mark.django_db
    def test_every_settings_page_is_reachable_from_the_nav(self, tenancy):
        """The guard for a page going quietly unreachable.

        The redesign briefly merged the Tags and Labels rows to match a design
        that drew one "Tags & labels": the merged row pointed at the tag list,
        which renders contact tags only, and the inbox Labels page was left with
        no link anywhere in the product. Nothing failed — the route still
        resolved, the view still worked, and the only way to find the page was
        to type its URL.

        Every settings route the product serves has to be some row's target.
        A row may cover several routes through ``url_names``, but a route no row
        points at is a page nobody can get to.
        """
        context = navigation_context(
            _request(f"/w/{tenancy.workspace.id}/", workspace=tenancy.workspace, user=tenancy.owner)
        )
        linked = {
            item["url"]
            for key in ("settings_nav_groups", "workspace_settings_nav_groups")
            for group in context[key]
            for item in group["items"]
        }

        for group in SETTINGS_NAV:
            for item in group.items:
                expected = item._url(tenancy.workspace.id)
                assert expected in linked, f"{item.key} ({item.label}) has no nav row pointing at it"

    def test_tags_and_labels_are_separate_destinations(self):
        """They are different models answering to different permissions, and the
        Labels page's own copy says so: a tag like "VIP" follows a person across
        every channel, a label like "waiting on shipping" is true of one thread.
        One row would have to lead somewhere that hides half of what it
        promises."""
        rows = {item.key: item for group in SETTINGS_NAV for item in group.items}

        assert rows["ws_tags"].url_name == "contacts:tag_list"
        assert rows["ws_labels"].url_name == "inbox:label_settings"

    def test_the_templates_tab_is_gated_like_the_page_behind_it(self):
        """Its two neighbours are readable by anybody in the workspace; this one
        is not. views_portability.template_gallery requires edit_flows, because
        picking a template writes a FlowImport row — so an ungated tab is a tab
        that answers 403 for an Agent while Flows and Sequences beside it work.
        """
        from apps.common.context_processors import FLOWS_TABS

        tabs = {item.key: item for group in FLOWS_TABS for item in group.items}

        assert tabs["tab_templates"].permission == "edit_flows"
        assert tabs["tab_flows"].permission == ""
        assert tabs["tab_sequences"].permission == ""

    def test_every_permission_a_row_names_is_a_real_permission_key(self):
        """A typo in NavItem.permission fails open in the worst direction: the
        key is never in effective_permissions, so the row silently vanishes for
        everyone and the page becomes unreachable again. The check lives here
        rather than in __post_init__ because these items are built at module
        import, before the app registry is guaranteed to be populated."""
        from apps.members.roles import PERMISSION_KEYS

        named = {i.permission for g in MAIN_NAV + SETTINGS_NAV for i in g.items if i.permission}

        assert named <= set(PERMISSION_KEYS), f"not permission keys: {sorted(named - set(PERMISSION_KEYS))}"

    def test_nav_item_keys_are_unique_across_both_navs(self):
        keys = [i.key for g in MAIN_NAV + SETTINGS_NAV for i in g.items]

        assert len(keys) == len(set(keys))

    def test_the_product_nav_is_the_one_the_design_specifies(self):
        """The encoded product decision, so changing it is a deliberate edit.

        Keys track the route and labels track the design — `dashboard`,
        `analytics` and `media` keep their keys while reading Home, Insights and
        Library, which is what keeps every cross-app test that looks a row up by
        key working through a rename.

        One row left the nav rather than the product: `sequences` became a tab
        on the flows page. `notifications` left for the app header and came
        back when the header was deleted — the sidebar is the surface that
        renders on every page now, and its row is the bell's trigger.
        """
        keys = [i.key for g in MAIN_NAV for i in g.items]

        assert set(keys) == {
            "dashboard",
            "inbox",
            "flows",
            "broadcasts",
            "contacts",
            "analytics",
            "notifications",
            "media",
            "settings",
        }

    def test_the_nav_is_split_into_a_top_and_a_bottom_group(self):
        """Library and Settings sit in the sidebar's footer, away from the rows
        that answer "what am I doing"."""
        top = [i.key for g in MAIN_NAV if g.placement == "top" for i in g.items]
        bottom = [i.key for g in MAIN_NAV if g.placement == "bottom" for i in g.items]

        assert top == ["dashboard", "inbox", "flows", "broadcasts", "contacts", "analytics", "notifications"]
        assert bottom == ["media", "settings"]

    def test_the_notifications_row_is_not_workspace_scoped(self):
        """A notification is addressed to a person and the feed spans every
        workspace they belong to, so this is the one main-nav row that survives
        a user whose workspaces are all archived — which is exactly when a
        channel_needs_reauth alert matters most."""
        row = next(i for g in MAIN_NAV for i in g.items if i.key == "notifications")

        assert row.workspace_scoped is False
        assert row.badge_key == "unread_notifications"

    def test_the_notifications_row_carries_the_partial_that_draws_it(self):
        """The row is the bell's trigger, not a plain anchor, so it renders
        itself. An include of a template that does not exist fails soft in
        Django — as an empty string — so the path is pinned here rather than
        discovered as a missing row on a page."""
        row = next(i for g in MAIN_NAV for i in g.items if i.key == "notifications")

        assert row.partial == "notifications/partials/_bell.html"
        assert (Path(__file__).parents[3] / "templates" / row.partial).exists()

    def test_a_row_marked_authenticated_only_is_dropped_for_an_anonymous_request(self):
        """sidebar_context returns {} for anonymous users, so this matters for
        exactly one caller: apps.common.views.ui_demo, which calls
        navigation_context directly for visitors with no session and promises
        /ui/ "reads no database and no session"."""
        context = navigation_context(_request("/ui/"))

        keys = [item["key"] for group in context["nav_groups"] for item in group["items"]]
        assert "notifications" not in keys

    def test_the_settings_row_lights_up_on_every_settings_page(self):
        """Derived from SETTINGS_NAV rather than hand-listed, so a settings page
        added later cannot leave the rail row dark."""
        settings_row = next(i for g in MAIN_NAV for i in g.items if i.key == "settings")
        every_settings_route = {
            name for g in SETTINGS_NAV for i in g.items for name in (i.url_names or frozenset({i.url_name}))
        }

        assert settings_row.url_names == every_settings_route

    def test_settings_groups_match_the_design(self):
        assert [g.label for g in SETTINGS_NAV] == ["Workspace", "Organisation", "You"]

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
        than no row.

        Settings survives, and should: it is per-user rather than
        workspace-scoped, so it still has somewhere real to point when the
        person has no current workspace — which is exactly when they need to
        reach the organisation's workspace list to bring one back.
        """
        context = navigation_context(_request("/organization/settings/", user=tenancy.owner))

        top = [item["key"] for group in context["nav_groups"] for item in group["items"]]
        bottom = [item["key"] for group in context["nav_footer_groups"] for item in group["items"]]
        # Notifications survives for the same reason Settings does, and more
        # sharply: it is addressed to the person, not the workspace, and a
        # channel_needs_reauth alert is most of what there is to read when
        # every workspace has been archived.
        assert top == ["notifications"]
        assert bottom == ["settings"]
        # The Workspace group empties for the same reason: no workspace means
        # no workspace membership, so none of its rows are visible either.
        # Organisation and You survive — they point somewhere real.
        assert [g["label"] for g in context["workspace_settings_nav_groups"]] == ["Organisation", "You"]

    def test_channel_connections_is_still_a_placeholder(self):
        """Issue #4 owns ChannelConnection; #31's credential store is
        per-platform configuration, not a connected account."""
        assert navigation_context(_request())["channel_connections"] == []

    def test_show_app_shell_is_set(self):
        assert navigation_context(_request())["show_app_shell"] is True


def _org_membership(user):
    from apps.members.models import OrgMembership

    return OrgMembership.objects.filter(user=user).first()
