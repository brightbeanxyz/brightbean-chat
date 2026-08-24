"""Roles, levels and the permission matrix — defined exactly once.

BrightBean Studio keeps three copies of its role-level maps: two inside
``apps/members/decorators.py`` and one in ``apps/members/services.py``, held in
sync by a ``# must match decorators.py`` comment. A permission system whose
authority ordering can disagree with itself is a permission system that will,
eventually, disagree with itself. Everything lives here and both modules import
it (brief, deviation 6).

The four workspace roles are SPEC §4's, not Studio's six: Studio's ``manager`` /
``contributor`` / ``client`` split serves an agency approval workflow this
product does not have, and its ``CustomRole`` escape hatch is dropped outright
(deviation 5) — it shipped with no UI, no service functions, and a
``require_workspace_role`` that ignored it despite a docstring claiming
otherwise.

``PERMISSION_KEYS`` and the four rows below are the whole authorization
vocabulary. Later layers gate on these keys; they do not invent new ones without
adding them here first.
"""

from django.db import models

__all__ = [
    "ORG_ROLE_LEVEL",
    "PERMISSION_KEYS",
    "ROLE_PERMISSIONS",
    "WORKSPACE_ROLE_LEVEL",
    "OrgRole",
    "WorkspaceRole",
    "permissions_for_role",
]


class OrgRole(models.TextChoices):
    """Authority over the organization itself: billing-free, but it owns
    workspaces, membership and the org-level credential store."""

    OWNER = "owner", "Owner"
    ADMIN = "admin", "Admin"
    MEMBER = "member", "Member"


class WorkspaceRole(models.TextChoices):
    """SPEC §4's four roles. Ordered admin > editor > agent > viewer."""

    ADMIN = "admin", "Admin"
    EDITOR = "editor", "Editor"
    AGENT = "agent", "Agent"
    VIEWER = "viewer", "Viewer"


ORG_ROLE_LEVEL: dict[str, int] = {
    OrgRole.OWNER: 3,
    OrgRole.ADMIN: 2,
    OrgRole.MEMBER: 1,
}

WORKSPACE_ROLE_LEVEL: dict[str, int] = {
    WorkspaceRole.ADMIN: 4,
    WorkspaceRole.EDITOR: 3,
    WorkspaceRole.AGENT: 2,
    WorkspaceRole.VIEWER: 1,
}

PERMISSION_KEYS: list[str] = [
    "use_inbox",
    "reply_in_inbox",
    "edit_contact_fields",
    "manage_crm",
    "edit_flows",
    "manage_media",
    "send_broadcasts",
    "manage_channels",
    "manage_members",
    "manage_workspace_settings",
    "manage_api_keys",
    "view_analytics",
    "erase_contacts",
]

# Admin-only keys. Editor is "admin minus these" — spelled as a subtraction so
# adding a permission key below can never silently grant it to Editor.
_ADMIN_ONLY_KEYS = frozenset(
    {
        "manage_channels",
        "manage_members",
        "manage_workspace_settings",
        "manage_api_keys",
        # GDPR erasure (SPEC §19, issue #29). Admin-only because it is the one
        # destructive act in the product with no undo: ``manage_crm``'s delete
        # sets ``status`` and every row survives, while this removes the person
        # and their message history outright. Editor holds the reversible half
        # and not this one.
        "erase_contacts",
    }
)

_AGENT_KEYS = frozenset({"use_inbox", "reply_in_inbox", "edit_contact_fields", "view_analytics"})

# Viewer is read-only everywhere (SPEC §4).
_VIEWER_KEYS = frozenset({"use_inbox", "view_analytics"})

ROLE_PERMISSIONS: dict[str, dict[str, bool]] = {
    WorkspaceRole.ADMIN: {key: True for key in PERMISSION_KEYS},
    WorkspaceRole.EDITOR: {key: key not in _ADMIN_ONLY_KEYS for key in PERMISSION_KEYS},
    WorkspaceRole.AGENT: {key: key in _AGENT_KEYS for key in PERMISSION_KEYS},
    WorkspaceRole.VIEWER: {key: key in _VIEWER_KEYS for key in PERMISSION_KEYS},
}


def permissions_for_role(role: str) -> dict[str, bool]:
    """Permissions for a built-in role.

    An unrecognised role denies everything rather than falling back to a
    permissive default: a role string that is not in the table means the
    database holds something this code does not understand, and guessing in the
    permissive direction is how a stale row becomes a privilege escalation.
    """
    return ROLE_PERMISSIONS.get(role, {key: False for key in PERMISSION_KEYS})
