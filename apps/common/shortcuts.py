"""View-layer helpers for tenant-scoped object access.

SECURITY-BASELINE §1: "Cross-workspace access to any object returns **404**
(never 403 — no existence oracle)." Django's own ``get_object_or_404`` goes
through ``Model._default_manager``, which for a
:class:`~apps.common.scoping.WorkspaceScopedModel` is the *plain* manager — so
calling it directly would look right and cross tenants. Use this instead.
"""

from typing import Any

from django.http import Http404


def get_scoped_object_or_404(model: Any, workspace: Any, **kwargs: Any) -> Any:
    """Fetch one object belonging to ``workspace``, or raise ``Http404``.

    404 rather than 403 whether the object belongs to another workspace or does
    not exist at all: the two must be indistinguishable, or the response itself
    tells an attacker which ids are real.
    """
    try:
        return model.objects.for_workspace(workspace).get(**kwargs)
    except (model.DoesNotExist, model.MultipleObjectsReturned, ValueError, TypeError) as exc:
        # ValueError/TypeError: a malformed pk (bad UUID) is a miss, not a 500.
        raise Http404(f"No {model.__name__} matches the given query.") from exc
