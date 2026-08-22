"""Turning "notify the workspace admins" into a list of people.

This is the part of issue #7 most likely to be got subtly wrong, because the
obvious query is only half of the answer.
"""

from collections.abc import Iterable, Sequence
from typing import Any

from django.contrib.auth import get_user_model

from apps.members.models import OrgMembership, WorkspaceMembership
from apps.members.roles import OrgRole, WorkspaceRole

__all__ = ["active_users", "recipients_for_roles"]

DEFAULT_ROLES: tuple[str, ...] = (WorkspaceRole.ADMIN,)


def recipients_for_roles(workspace: Any, roles: Sequence[str]) -> list[Any]:
    """Every active user holding one of ``roles`` in ``workspace``.

    ``WorkspaceMembership.objects.filter(workspace=..., workspace_role__in=...)``
    looks like the whole query and is not. **SPEC §4.2: "an org owner is treated
    as a workspace admin in every workspace of their org."** An org owner need
    hold no ``WorkspaceMembership`` row at all — ``apps.members.services``
    ``.workspace_authority_map`` grants them workspace-admin authority from the
    organization side, without one. Ask only the membership table and the loop
    cap notifies nobody in the common case of a solo owner who never bothered to
    add themselves to their own workspace.

    So when ``admin`` is among the requested roles, org owners are unioned in.
    The other three roles have no organization-tier equivalent: an org *admin*
    gets no implicit workspace authority, only what their own membership grants.

    Note ``WorkspaceMembership`` has no status or ``is_active`` column — the row
    existing *is* the membership. Liveness is a property of the user, so that is
    where it is checked.
    """
    unknown = [role for role in roles if role not in WorkspaceRole.values]
    if unknown:
        raise ValueError(
            f"Unknown workspace role(s) {unknown!r}. Roles come from "
            f"apps.members.roles.WorkspaceRole: {WorkspaceRole.values}."
        )

    user_ids = set(
        WorkspaceMembership.objects.filter(
            workspace=workspace,
            workspace_role__in=list(roles),
        ).values_list("user_id", flat=True)
    )

    if WorkspaceRole.ADMIN in roles:
        user_ids |= set(
            OrgMembership.objects.filter(
                organization_id=workspace.organization_id,
                org_role=OrgRole.OWNER,
            ).values_list("user_id", flat=True)
        )

    if not user_ids:
        return []
    return list(get_user_model().objects.filter(pk__in=user_ids, is_active=True).order_by("email"))


def active_users(users: Iterable[Any]) -> list[Any]:
    """Deduplicate an explicit recipient list and drop deactivated accounts.

    Callers pass whatever they have — a queryset, a list with the same person in
    it twice because they are both the assignee and a mentioned member. Neither
    should produce two notifications.
    """
    seen: set[Any] = set()
    result: list[Any] = []
    for user in users:
        if user is None or not getattr(user, "is_active", False):
            continue
        if user.pk in seen:
            continue
        seen.add(user.pk)
        result.append(user)
    return result
