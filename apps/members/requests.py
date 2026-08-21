"""The request shapes ``RBACMiddleware`` guarantees.

Django attaches middleware state to the request dynamically, which is convenient
and invisible to a type checker: every ``request.workspace`` in a view is an
``attr-defined`` error against the stock ``HttpRequest``.

There are three shapes rather than one because the decorators narrow the
contract, and the signature is the right place to say which one a view is
relying on:

``RBACRequest``
    What every request looks like. All four attributes may be ``None`` —
    anonymous users included.
``OrgRequest``
    After ``@login_required`` + ``@require_org_role(...)``: the organization and
    the caller's membership in it are resolved.
``WorkspaceRequest``
    After a ``/w/<uuid:workspace_id>/`` route: the middleware resolved the
    workspace and the caller's membership, or already answered 404.

They are siblings, not a hierarchy: narrowing a mutable attribute from
``X | None`` to ``X`` in a subclass is unsound in general and mypy rejects it,
so each states its own contract.
"""

from typing import TYPE_CHECKING, Any

from django.http import HttpRequest

if TYPE_CHECKING:
    from apps.members.models import OrgMembership, WorkspaceMembership
    from apps.organizations.models import Organization
    from apps.workspaces.models import Workspace

# ``user`` is not narrowed from django-stubs' ``AbstractBaseUser | AnonymousUser``:
# that would be an invariance error on a mutable attribute, and every view using
# these types is behind ``@login_required`` anyway.


class RBACRequest(HttpRequest):
    """Any request, after the middleware has run."""

    org: "Organization | None"
    org_membership: "OrgMembership | None"
    workspace: "Workspace | None"
    workspace_membership: "WorkspaceMembership | None"
    user: Any


class OrgRequest(HttpRequest):
    """A request that passed ``@require_org_role(...)``."""

    org: "Organization"
    org_membership: "OrgMembership"
    workspace: "Workspace | None"
    workspace_membership: "WorkspaceMembership | None"
    user: Any


class WorkspaceRequest(HttpRequest):
    """A request on a ``/w/<uuid:workspace_id>/`` route.

    ``org`` is resolved from the workspace, so it is non-optional here too: a
    workspace always belongs to an organization, and the middleware looks the
    caller's membership in it up before the view runs.
    """

    org: "Organization"
    org_membership: "OrgMembership"
    workspace: "Workspace"
    workspace_membership: "WorkspaceMembership"
    user: Any
