"""Permission decorators for RBAC enforcement.

Ported from BrightBean Studio's ``apps/members/decorators.py``, with the level
maps imported from :mod:`apps.members.roles` instead of re-declared (deviation
6) and with one behavioural fix: Studio resolves an unknown ``min_role``
argument to level 0, so ``@require_org_role("admn")`` lets *everyone* through —
a typo that fails open, silently, forever. Here the role is validated when the
decorator is applied, so a bad argument is an ImportError-time crash.

None of these resolve anything from the URL: ``RBACMiddleware`` has already put
``request.org_membership`` and ``request.workspace_membership`` on the request.

**Stacking convention** (outermost first), used at every call site::

    @login_required
    @require_permission("manage_members")
    @require_POST
    def some_view(request, workspace_id): ...

The order matters for more than tidiness. ``require_POST`` innermost means the
tenancy and permission checks run *before* the method check, so a GET against a
POST-only view from another tenant answers 404 rather than 405 — a 405 would
confirm the route and the object exist.

Failures here are ``PermissionDenied`` → 403, which is correct: "you are in this
workspace but lack the permission" reveals nothing an attacker did not already
know. Cross-*tenant* access is a different question and answers 404, in
``RBACMiddleware`` and ``get_scoped_object_or_404`` (SECURITY-BASELINE §1).
"""

import functools
from collections.abc import Callable
from typing import Any

from django.core.exceptions import PermissionDenied

from apps.members.roles import ORG_ROLE_LEVEL, PERMISSION_KEYS, WORKSPACE_ROLE_LEVEL

__all__ = ["require_org_role", "require_permission", "require_workspace_role"]

ViewFunc = Callable[..., Any]


def require_permission(permission_key: str) -> Callable[[ViewFunc], ViewFunc]:
    """Require one permission key from the current workspace membership.

    Reads ``effective_permissions`` and nothing else, so anything that exposes
    that mapping authorizes here — including the public API's bearer-token
    membership shim (#25).
    """
    if permission_key not in PERMISSION_KEYS:
        raise ValueError(f"Unknown permission key {permission_key!r}. Add it to apps.members.roles.PERMISSION_KEYS.")

    def decorator(view_func: ViewFunc) -> ViewFunc:
        @functools.wraps(view_func)
        def _wrapped(request: Any, *args: Any, **kwargs: Any) -> Any:
            membership = getattr(request, "workspace_membership", None)
            if not membership:
                raise PermissionDenied("You are not a member of this workspace.")
            if not membership.effective_permissions.get(permission_key, False):
                # The key is named because it is not a secret: it is in this
                # repository, and telling an operator which permission they are
                # missing is the difference between a fixable error and a
                # support ticket.
                raise PermissionDenied(f"Permission denied: {permission_key}")
            return view_func(request, *args, **kwargs)

        return _wrapped

    return decorator


def require_workspace_role(min_role: str) -> Callable[[ViewFunc], ViewFunc]:
    """Require at least ``min_role`` in the current workspace.

    Prefer :func:`require_permission`: it is the protocol the API shim
    duck-types against, while this one reads ``workspace_role`` off a real
    membership row. Use it only where the gate really is seniority rather than a
    named capability.
    """
    if min_role not in WORKSPACE_ROLE_LEVEL:
        raise ValueError(f"Unknown workspace role {min_role!r}. Expected one of {sorted(WORKSPACE_ROLE_LEVEL)}.")
    required_level = WORKSPACE_ROLE_LEVEL[min_role]

    def decorator(view_func: ViewFunc) -> ViewFunc:
        @functools.wraps(view_func)
        def _wrapped(request: Any, *args: Any, **kwargs: Any) -> Any:
            membership = getattr(request, "workspace_membership", None)
            if not membership:
                raise PermissionDenied("You are not a member of this workspace.")
            if WORKSPACE_ROLE_LEVEL.get(getattr(membership, "workspace_role", ""), 0) < required_level:
                raise PermissionDenied("Insufficient workspace role.")
            return view_func(request, *args, **kwargs)

        return _wrapped

    return decorator


def require_org_role(min_role: str) -> Callable[[ViewFunc], ViewFunc]:
    """Require at least ``min_role`` in the user's organization."""
    if min_role not in ORG_ROLE_LEVEL:
        raise ValueError(f"Unknown org role {min_role!r}. Expected one of {sorted(ORG_ROLE_LEVEL)}.")
    required_level = ORG_ROLE_LEVEL[min_role]

    def decorator(view_func: ViewFunc) -> ViewFunc:
        @functools.wraps(view_func)
        def _wrapped(request: Any, *args: Any, **kwargs: Any) -> Any:
            membership = getattr(request, "org_membership", None)
            if not membership:
                raise PermissionDenied("You are not a member of any organization.")
            if ORG_ROLE_LEVEL.get(membership.org_role, 0) < required_level:
                raise PermissionDenied("Insufficient organization role.")
            return view_func(request, *args, **kwargs)

        return _wrapped

    return decorator
