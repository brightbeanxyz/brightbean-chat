"""The lists the builder's config panels choose from (SPEC §16).

``GET /…/api/flows/<id>/`` returns tags, custom fields, sequences, flows,
connections and members so a panel can render a dropdown instead of asking the
author to type a tag name and hope.

Half of those live in apps that have not been built yet. Each resolver therefore
answers ``[]`` rather than raising, and the payload always carries **every** key:
a client that has to branch on which keys are present is a client that will
break on the day one of them starts arriving. The stubs, and what fills them:

===============  ===============================================
``flows``        real — this app
``members``      real — ``apps.members``
``tags``         ``[]`` until #3 (L2-A) ships ``contacts.Tag``
``custom_fields````[]`` until #3 ships ``contacts.CustomField``
``connections``  ``[]`` until #4 (L2-B) ships ``ChannelConnection``
``sequences``    ``[]`` until L6-A ships ``campaigns.Sequence``
===============  ===============================================

Every entry is ``{"id", "label"}`` plus whatever else that kind needs, so the
panels share one renderer.
"""

from collections.abc import Callable
from typing import Any

from apps.flows.compat import installed_model

__all__ = ["PICKLIST_RESOLVERS", "picklists"]


def _flows(workspace: Any) -> list[dict[str, Any]]:
    """Targets for the start_flow node. Archived flows are not offered."""
    from apps.flows.models import Flow, FlowStatus

    rows = (
        Flow.objects.for_workspace(workspace)
        .exclude(status=FlowStatus.ARCHIVED)
        .order_by("name")
        .values("id", "name", "status")
    )
    return [{"id": str(row["id"]), "label": row["name"], "status": row["status"]} for row in rows]


def _members(workspace: Any) -> list[dict[str, Any]]:
    """Workspace members, for assign_conversation and notify_members.

    Keyed by **user** id, not membership id: ``conversation.assignee`` is a user
    (SPEC §5), so a graph that stored a membership id would break the moment
    someone's membership row was recreated.
    """
    from apps.members.models import WorkspaceMembership

    memberships = WorkspaceMembership.objects.filter(workspace=workspace).select_related("user").order_by("user__email")
    return [
        {
            "id": str(membership.user_id),
            "label": membership.user.name or membership.user.email,
            "email": membership.user.email,
            "role": membership.workspace_role,
        }
        for membership in memberships
    ]


def _tags(workspace: Any) -> list[dict[str, Any]]:
    model = installed_model("contacts", "apps.contacts", "Tag")
    if model is None:
        return []
    return [
        {"id": str(row["id"]), "label": row["name"]}
        for row in model.objects.for_workspace(workspace).order_by("name").values("id", "name")
    ]


def _custom_fields(workspace: Any) -> list[dict[str, Any]]:
    model = installed_model("contacts", "apps.contacts", "CustomField")
    if model is None:
        return []
    return [
        {"id": str(row["id"]), "label": row["name"], "type": row["type"]}
        for row in model.objects.for_workspace(workspace).order_by("name").values("id", "name", "type")
    ]


def _connections(workspace: Any) -> list[dict[str, Any]]:
    model = installed_model("channels", "apps.channels", "ChannelConnection")
    if model is None:
        return []
    return [
        {"id": str(row["id"]), "label": row["display_name"], "platform": row["platform"], "status": row["status"]}
        for row in model.objects.for_workspace(workspace)
        .order_by("display_name")
        .values("id", "display_name", "platform", "status")
    ]


def _sequences(workspace: Any) -> list[dict[str, Any]]:
    """Documented stub: campaigns.Sequence arrives with L6-A (issue #22)."""
    model = installed_model("campaigns", "apps.campaigns", "Sequence")
    if model is None:
        return []
    return [
        {"id": str(row["id"]), "label": row["name"]}
        for row in model.objects.for_workspace(workspace).order_by("name").values("id", "name")
    ]


#: The keys the API always returns, in a fixed order. A later app fills its own
#: entry by existing; nothing here changes when it does.
PICKLIST_RESOLVERS: dict[str, Callable[[Any], list[dict[str, Any]]]] = {
    "tags": _tags,
    "custom_fields": _custom_fields,
    "sequences": _sequences,
    "flows": _flows,
    "connections": _connections,
    "members": _members,
}


def picklists(workspace: Any) -> dict[str, list[dict[str, Any]]]:
    """Every pick-list for this workspace, stubs included."""
    return {key: resolver(workspace) for key, resolver in PICKLIST_RESOLVERS.items()}
