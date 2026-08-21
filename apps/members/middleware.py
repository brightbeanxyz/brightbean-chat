"""RBAC middleware: resolves org and workspace context on every request.

Ported from BrightBean Studio's ``apps/members/middleware.py`` with three
deliberate changes.

**1. A workspace you cannot reach answers 404, not 403.** Studio raises
``PermissionDenied``. SECURITY-BASELINE §1 requires cross-workspace access to be
indistinguishable from "no such thing": a 403 confirms the id names a real
workspace, which is an existence oracle over a UUID space that is otherwise
unguessable.

**2. Archived workspaces are unreachable through the URL too.** Studio filters
``is_archived=False`` on the ``last_workspace_id`` fallback but not on the
URL-kwarg path, so an archived workspace stays fully usable to anyone holding a
direct link — the archive is a filter on one navigation path rather than a
state. Both paths filter it now; unarchiving happens from the org-level
workspace list, which is org-scoped and lists archived ones.

**3. ``last_workspace_id`` is only written when it actually changed**, and never
on a request that is not workspace-scoped.

Carried forward deliberately: **v1 assumes one organization per user.** The org
lookup is ``.first()`` over the user's memberships, which is well-defined only
because signup provisions exactly one org and invitations join exactly one. If
multi-org arrives, this is the function that has to resolve org from the URL or
the session instead — and every ``request.org`` consumer inherits the change.

Resolution happens in ``process_view`` rather than ``__call__`` because the URL
kwargs are what identify the workspace, and those do not exist until Django has
resolved the route.
"""

from collections.abc import Callable
from typing import Any

from django.http import Http404, HttpRequest, HttpResponse

from apps.members.models import OrgMembership, WorkspaceMembership

WORKSPACE_URL_KWARG = "workspace_id"


class RBACMiddleware:
    """Attach ``org``, ``org_membership``, ``workspace`` and
    ``workspace_membership`` to every request.

    All four are always set — ``None`` for anonymous users — so decorators and
    templates can test them without ``hasattr`` dances.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        request.org = None  # type: ignore[attr-defined]
        request.org_membership = None  # type: ignore[attr-defined]
        request.workspace = None  # type: ignore[attr-defined]
        request.workspace_membership = None  # type: ignore[attr-defined]

        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            org_membership = (
                OrgMembership.objects.filter(user=user).select_related("organization").order_by("created_at").first()
            )
            if org_membership:
                request.org = org_membership.organization  # type: ignore[attr-defined]
                request.org_membership = org_membership  # type: ignore[attr-defined]

            # Fallback for pages with no workspace in the URL (the org settings
            # pages, the root router) so the switcher still knows where it is.
            # process_view overrides this for workspace-scoped routes.
            if getattr(user, "last_workspace_id", None):
                membership = self._membership_for(user, user.last_workspace_id)
                if membership:
                    request.workspace = membership.workspace  # type: ignore[attr-defined]
                    request.workspace_membership = membership  # type: ignore[attr-defined]

        return self.get_response(request)

    def process_view(
        self,
        request: HttpRequest,
        view_func: Callable[..., HttpResponse],
        view_args: tuple[Any, ...],
        view_kwargs: dict[str, Any],
    ) -> HttpResponse | None:
        """Resolve the workspace named by the URL, or 404."""
        workspace_id = view_kwargs.get(WORKSPACE_URL_KWARG)
        if not workspace_id:
            return None

        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            # Anonymous requests fall through to @login_required, which
            # redirects to the login page. Answering 404 here instead would
            # break every "log in and come back" link.
            return None

        # Clear the last_workspace_id fallback before resolving, so a failed
        # lookup cannot leave the previous workspace's context attached.
        request.workspace = None  # type: ignore[attr-defined]
        request.workspace_membership = None  # type: ignore[attr-defined]

        membership = self._membership_for(user, workspace_id)
        if membership is None:
            raise Http404("No such workspace.")

        request.workspace = membership.workspace  # type: ignore[attr-defined]
        request.workspace_membership = membership  # type: ignore[attr-defined]

        if user.last_workspace_id != membership.workspace_id:
            user.last_workspace_id = membership.workspace_id
            user.save(update_fields=["last_workspace_id"])

        org = membership.workspace.organization
        org_membership = (
            OrgMembership.objects.filter(user=user, organization=org).select_related("organization").first()
        )
        if org_membership:
            request.org = org_membership.organization  # type: ignore[attr-defined]
            request.org_membership = org_membership  # type: ignore[attr-defined]

        return None

    @staticmethod
    def _membership_for(user: Any, workspace_id: Any) -> WorkspaceMembership | None:
        """The user's membership in a live workspace, or None.

        A malformed id is a miss, not a 500: ``workspace_id`` arrives from a
        ``<uuid:...>`` converter on the URL routes, but the fallback path reads
        it from a column that predates the workspace being deleted.
        """
        try:
            return (
                WorkspaceMembership.objects.filter(
                    user=user,
                    workspace_id=workspace_id,
                    workspace__is_archived=False,
                )
                .select_related("workspace__organization")
                .first()
            )
        except (ValueError, TypeError):
            return None
