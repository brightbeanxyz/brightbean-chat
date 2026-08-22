"""SECURITY-BASELINE §1: cross-tenant access answers 404, on every route."""

import pytest
from django.test import override_settings

from tests.idor import (
    TENANT_KWARG_RESOLVERS,
    WAIVED_ROUTES,
    UnnamedTenantRouteError,
    UnregisteredRouteKwargError,
    assert_cross_tenant_isolation,
    iter_tenant_routes,
)


@pytest.mark.django_db
class TestCrossTenantIsolation:
    def test_every_tenant_route_404s_for_an_outsider(self, tenancy, other_tenancy, client_for):
        """The attacker is an org **owner** and workspace admin in their own org.

        Maximum privilege on their own side and none on the victim's, so a
        refusal can only come from tenancy — a low-privilege outsider would be
        stopped by their role and prove nothing.
        """
        assert_cross_tenant_isolation(client_for(other_tenancy.owner), tenancy)

    def test_the_sweep_covers_the_routes_this_issue_added(self):
        names = {route.name for route in iter_tenant_routes()}

        assert {
            "workspaces:dashboard",
            "workspaces:switch",
            "workspaces:settings",
            "workspaces:update_settings",
            "credentials:list",
            "credentials:edit",
            "credentials:clear",
            "members:update_role",
            "members:remove",
            "members:manage_workspaces",
            "members:resend_invite",
            "members:revoke_invite",
            "organizations:set_workspace_archived",
        } <= names

    def test_the_sweep_covers_the_notification_route(self):
        """Issue #7's route is per-user rather than workspace-scoped, so
        iter_tenant_routes() would skip it silently — it `continue`s on a route
        carrying no *registered* tenant kwarg before it ever reaches the
        unknown-kwarg check. Registering `notification_id` opts it in, and this
        assertion is what stops a later edit from quietly opting it back out.
        """
        names = {route.name for route in iter_tenant_routes()}

        assert "notifications:mark_read" in names

    def test_the_sweep_covers_the_shell_placeholders(self):
        """Issue #32's sidebar destinations are real endpoints under
        /w/<uuid>/, so baseline §1 binds them like any other: a member of
        another workspace must get a 404. They are placeholders today, but the
        route and its guard outlive the placeholder."""
        names = {route.name for route in iter_tenant_routes()}

        assert {
            "inbox",
            "sequences",
            "broadcasts",
        } <= names

    def test_the_sweep_covers_the_contacts_routes(self):
        """Issue #3 replaced three of the shell placeholders with real views."""
        names = {route.name for route in iter_tenant_routes()}

        assert {
            "contacts:list",
            "contacts:tag_list",
            "contacts:tag_rows",
            "contacts:tag_create",
            "contacts:tag_rename",
            "contacts:tag_delete",
            "contacts:field_list",
            "contacts:field_rows",
            "contacts:field_create",
            "contacts:field_rename",
            "contacts:field_delete",
        } <= names

    def test_the_sweep_covers_the_flows_app(self):
        """Issue #6 replaced the `flows` placeholder with the real app, pages and
        builder data API alike. Every one of those routes names a flow, so the
        sweep has to reach all of them — including the API, which is exactly the
        surface a placeholder never had."""
        names = {route.name for route in iter_tenant_routes()}

        assert {
            "flows:list",
            "flows:create",
            "flows:edit",
            "flows:rename",
            "flows:duplicate",
            "flows:archive",
            "flows:restore",
            "flows:api_detail",
            "flows:api_publish",
            "flows:api_stats",
            "flows:api_schema",
        } <= names

    def test_the_sweep_covers_the_trigger_routes(self):
        """Issue #11's Triggers panel.

        Eight partial routes and one image, all naming a flow and most naming a
        trigger, so every one of them is a place a trigger id from another
        workspace could be handed in. The QR endpoint is the sharpest of them:
        it renders bytes rather than a page, and it takes a *connection* id
        beside the trigger id, so it is the one route here that could leak one
        tenant's deep link into another tenant's response.
        """
        names = {route.name for route in iter_tenant_routes()}

        assert {
            "flows:trigger_panel",
            "flows:trigger_form",
            "flows:trigger_create",
            "flows:trigger_update",
            "flows:trigger_toggle",
            "flows:trigger_move",
            "flows:trigger_delete",
            "flows:trigger_qr",
        } <= names

    def test_every_waiver_states_a_reason(self):
        assert all(reason.strip() for reason in WAIVED_ROUTES.values())


@pytest.mark.django_db
class TestTheSuiteActuallyCatchesLeaks:
    """Deviation 3: prove the helper fails on a deliberately-broken view."""

    @override_settings(ROOT_URLCONF="tests.broken_urls")
    def test_a_leaky_view_is_reported(self, tenancy, other_tenancy, client_for):
        with pytest.raises(AssertionError) as caught:
            assert_cross_tenant_isolation(client_for(other_tenancy.owner), tenancy, urlconf="tests.broken_urls")

        message = str(caught.value)
        assert "leaky" in message
        assert "200" in message

    def test_an_unregistered_tenant_kwarg_is_an_error_not_a_skip(self, monkeypatch):
        """A new endpoint must extend the registry, not slip past it."""
        monkeypatch.delitem(TENANT_KWARG_RESOLVERS, "platform", raising=False)
        monkeypatch.setitem(TENANT_KWARG_RESOLVERS, "workspace_id", lambda t: t.workspace.pk)
        monkeypatch.setattr("tests.idor.NEUTRAL_KWARG_VALUES", {})

        with pytest.raises(UnregisteredRouteKwargError) as caught:
            iter_tenant_routes()

        assert "platform" in str(caught.value)

    def test_an_unnamed_tenant_route_is_an_error_not_a_skip(self):
        """A route nothing reverses is exactly the kind that gets registered
        without a name; skipping it would be the one silent hole in a mechanism
        whose whole point is that nothing escapes quietly."""
        with (
            override_settings(ROOT_URLCONF="tests.unnamed_urls"),
            pytest.raises(UnnamedTenantRouteError) as caught,
        ):
            iter_tenant_routes("tests.unnamed_urls")

        assert "no name=" in str(caught.value)

    def test_an_unnamed_route_without_tenant_ids_is_fine(self):
        """Only tenant-addressable routes have to be reversible here."""
        assert iter_tenant_routes("tests.unnamed_urls_harmless") == []
