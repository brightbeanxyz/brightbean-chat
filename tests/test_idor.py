"""SECURITY-BASELINE §1: cross-tenant access answers 404, on every route."""

import pytest
from django.test import override_settings

from tests.idor import (
    TENANT_KWARG_RESOLVERS,
    WAIVED_ROUTES,
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
