"""SPEC §4's four roles and their exact permission table."""

import pytest

from apps.members.roles import (
    ORG_ROLE_LEVEL,
    PERMISSION_KEYS,
    ROLE_PERMISSIONS,
    WORKSPACE_ROLE_LEVEL,
    OrgRole,
    WorkspaceRole,
    permissions_for_role,
)

# The table from the brief, written out independently of the implementation so
# the test would fail if the derivation logic changed meaning.
EXPECTED_TRUE = {
    "admin": set(PERMISSION_KEYS),
    "editor": {
        "use_inbox",
        "reply_in_inbox",
        "edit_contact_fields",
        "manage_crm",
        "edit_flows",
        "send_broadcasts",
        "view_analytics",
    },
    "agent": {"use_inbox", "reply_in_inbox", "edit_contact_fields", "view_analytics"},
    "viewer": {"use_inbox", "view_analytics"},
}


class TestTheFourRoles:
    def test_there_are_exactly_four(self):
        """Studio has six plus a CustomRole escape hatch; both are dropped."""
        assert [role.value for role in WorkspaceRole] == ["admin", "editor", "agent", "viewer"]

    def test_custom_roles_do_not_exist(self):
        """Deviation 5: Studio's CustomRole ships with no UI and is ignored by
        require_workspace_role despite its docstring."""
        from apps.members import models

        assert not hasattr(models, "CustomRole")

    def test_the_hierarchy_is_strictly_ordered(self):
        assert WORKSPACE_ROLE_LEVEL == {"admin": 4, "editor": 3, "agent": 2, "viewer": 1}

    def test_org_roles_are_ordered_too(self):
        assert ORG_ROLE_LEVEL == {"owner": 3, "admin": 2, "member": 1}


class TestThePermissionTable:
    @pytest.mark.parametrize("role", ["admin", "editor", "agent", "viewer"])
    def test_matches_the_specification(self, role):
        granted = {key for key, allowed in ROLE_PERMISSIONS[role].items() if allowed}

        assert granted == EXPECTED_TRUE[role]

    @pytest.mark.parametrize("role", ["admin", "editor", "agent", "viewer"])
    def test_every_role_answers_every_key(self, role):
        """A missing key would resolve to False by .get() default, which is the
        right answer for the wrong reason — and invisible when a key is added."""
        assert set(ROLE_PERMISSIONS[role]) == set(PERMISSION_KEYS)

    def test_editor_is_admin_minus_the_admin_only_keys(self):
        admin_only = {"manage_channels", "manage_members", "manage_workspace_settings", "manage_api_keys"}

        assert EXPECTED_TRUE["admin"] - EXPECTED_TRUE["editor"] == admin_only

    def test_viewer_is_read_only(self):
        """SPEC §4: 'read-only everywhere'."""
        writes = {
            "reply_in_inbox",
            "edit_contact_fields",
            "manage_crm",
            "edit_flows",
            "send_broadcasts",
            "manage_channels",
            "manage_members",
            "manage_workspace_settings",
            "manage_api_keys",
        }

        assert not (EXPECTED_TRUE["viewer"] & writes)

    def test_only_admin_manages_members(self):
        holders = {role for role, keys in EXPECTED_TRUE.items() if "manage_members" in keys}

        assert holders == {"admin"}

    def test_an_unknown_role_denies_everything(self):
        """A stale role string in the database must not fail open."""
        assert permissions_for_role("owner") == dict.fromkeys(PERMISSION_KEYS, False)


class TestSingleSourceOfTruth:
    def test_decorators_and_services_share_one_map(self):
        """Deviation 6: Studio keeps three copies kept in sync by a comment."""
        from apps.members import decorators, services

        assert decorators.WORKSPACE_ROLE_LEVEL is WORKSPACE_ROLE_LEVEL
        assert services.WORKSPACE_ROLE_LEVEL is WORKSPACE_ROLE_LEVEL
        assert decorators.ORG_ROLE_LEVEL is ORG_ROLE_LEVEL
        assert services.ORG_ROLE_LEVEL is ORG_ROLE_LEVEL

    def test_org_roles_are_defined_once(self):
        from apps.members import models

        assert models.OrgRole is OrgRole
