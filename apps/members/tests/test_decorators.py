"""The permission decorators — the reusable gate every later layer uses."""

from dataclasses import dataclass
from typing import Any

import pytest
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.test import RequestFactory

from apps.members.decorators import require_org_role, require_permission, require_workspace_role
from apps.members.roles import permissions_for_role

rf = RequestFactory()


@dataclass
class FakeMembership:
    """The public API (#25) authorizes by duck-typing exactly this shape.

    ``effective_permissions`` is the sole protocol, which is why
    ``require_permission`` reads nothing else.
    """

    workspace_role: str = ""

    @property
    def effective_permissions(self) -> dict[str, bool]:
        return permissions_for_role(self.workspace_role)


def _request(*, workspace_membership: Any = None, org_membership: Any = None) -> Any:
    request: Any = rf.get("/")
    request.workspace_membership = workspace_membership
    request.org_membership = org_membership
    return request


def view(request, *args, **kwargs):
    return HttpResponse("ok")


class TestRequirePermission:
    @pytest.mark.parametrize(
        ("role", "allowed"),
        [("admin", True), ("editor", True), ("agent", False), ("viewer", False)],
    )
    def test_edit_flows_is_editor_and_above(self, role, allowed):
        """'Agent blocked from flow routes' — the flow routes themselves arrive
        in Layer 2, but the gate they will use is this one."""
        guarded = require_permission("edit_flows")(view)
        request = _request(workspace_membership=FakeMembership(role))

        if allowed:
            assert guarded(request).status_code == 200
        else:
            with pytest.raises(PermissionDenied):
                guarded(request)

    @pytest.mark.parametrize(
        ("role", "allowed"),
        [("admin", True), ("editor", False), ("agent", False), ("viewer", False)],
    )
    def test_manage_members_is_admin_only(self, role, allowed):
        guarded = require_permission("manage_members")(view)
        request = _request(workspace_membership=FakeMembership(role))

        if allowed:
            assert guarded(request).status_code == 200
        else:
            with pytest.raises(PermissionDenied):
                guarded(request)

    def test_a_non_member_is_refused(self):
        with pytest.raises(PermissionDenied):
            require_permission("use_inbox")(view)(_request())

    def test_it_only_reads_effective_permissions(self):
        """Anything exposing the mapping authorizes — that is the #25 contract."""

        class OnlyPermissions:
            effective_permissions = {"use_inbox": True}

        assert (
            require_permission("use_inbox")(view)(_request(workspace_membership=OnlyPermissions())).status_code == 200
        )

    def test_an_unknown_key_is_rejected_at_decoration_time(self):
        with pytest.raises(ValueError, match="Unknown permission key"):
            require_permission("delete_the_internet")


class TestRequireWorkspaceRole:
    @pytest.mark.parametrize(
        ("role", "allowed"),
        [("admin", True), ("editor", True), ("agent", False), ("viewer", False)],
    )
    def test_minimum_role_is_inclusive(self, role, allowed):
        guarded = require_workspace_role("editor")(view)
        request = _request(workspace_membership=FakeMembership(role))

        if allowed:
            assert guarded(request).status_code == 200
        else:
            with pytest.raises(PermissionDenied):
                guarded(request)

    def test_an_unknown_role_argument_fails_closed_at_decoration_time(self):
        """Studio resolves an unknown min_role to level 0, so a typo lets
        everyone through — silently, and forever."""
        with pytest.raises(ValueError, match="Unknown workspace role"):
            require_workspace_role("admn")

    def test_an_unknown_stored_role_denies(self):
        with pytest.raises(PermissionDenied):
            require_workspace_role("viewer")(view)(_request(workspace_membership=FakeMembership("manager")))


class TestRequireOrgRole:
    @dataclass
    class FakeOrgMembership:
        org_role: str

    @pytest.mark.parametrize(
        ("role", "allowed"),
        [("owner", True), ("admin", True), ("member", False)],
    )
    def test_minimum_role_is_inclusive(self, role, allowed):
        guarded = require_org_role("admin")(view)
        request = _request(org_membership=self.FakeOrgMembership(role))

        if allowed:
            assert guarded(request).status_code == 200
        else:
            with pytest.raises(PermissionDenied):
                guarded(request)

    def test_a_user_with_no_org_is_refused(self):
        with pytest.raises(PermissionDenied):
            require_org_role("member")(view)(_request())

    def test_an_unknown_role_argument_fails_closed_at_decoration_time(self):
        with pytest.raises(ValueError, match="Unknown org role"):
            require_org_role("superuser")
