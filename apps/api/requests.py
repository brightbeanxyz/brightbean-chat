"""The request shape ``ApiKeyAuth`` guarantees.

``apps/members/requests.py`` does the same job for the middleware-populated
request; this is its bearer-token sibling. A Ninja operation runs after
authentication, so by the time a route body sees the request these three
attributes are set — and saying so in the signature is what lets mypy check a
router at all.

Not a subclass of ``WorkspaceRequest``: ``workspace_membership`` here is a
:class:`~apps.api.auth.VirtualMembership`, not a ``WorkspaceMembership`` row,
and narrowing a mutable attribute to a different type in a subclass is unsound.
They are siblings for the same reason the three in ``apps.members.requests`` are.
"""

from typing import TYPE_CHECKING, Any

from django.http import HttpRequest

if TYPE_CHECKING:
    from apps.api.auth import VirtualMembership
    from apps.api.models import ApiKey
    from apps.workspaces.models import Workspace

__all__ = ["ApiRequest"]


class ApiRequest(HttpRequest):
    """A request that passed ``ApiKeyAuth``."""

    api_key: "ApiKey"
    workspace: "Workspace"
    workspace_membership: "VirtualMembership"
    #: Ninja sets this to whatever the auth callback returned — the key row.
    auth: Any
