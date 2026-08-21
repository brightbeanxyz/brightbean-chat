"""View-layer helpers for tenant-scoped object access.

SECURITY-BASELINE §1: "Cross-workspace access to any object returns **404**
(never 403 — no existence oracle)." Django's own ``get_object_or_404`` goes
through ``Model._default_manager``, which for a
:class:`~apps.common.scoping.WorkspaceScopedModel` is the *plain* manager — so
calling it directly would look right and cross tenants. Use this instead.
"""

from typing import Any

from django.core.exceptions import ValidationError
from django.http import Http404


def get_scoped_object_or_404(model: Any, workspace: Any, **kwargs: Any) -> Any:
    """Fetch one object belonging to ``workspace``, or raise ``Http404``.

    404 rather than 403 whether the object belongs to another workspace or does
    not exist at all: the two must be indistinguishable, or the response itself
    tells an attacker which ids are real.
    """
    # Scoping happens OUTSIDE the try. for_workspace(None) raises ValueError on
    # purpose — it means the caller has no workspace in hand and their filter
    # would match nothing — and catching that here would serve a programming
    # error as an ordinary 404, which is exactly the silence the guard exists to
    # break.
    queryset = model.objects.for_workspace(workspace)
    try:
        return queryset.get(**kwargs)
    except (model.DoesNotExist, model.MultipleObjectsReturned, ValidationError, ValueError, TypeError) as exc:
        # ValidationError/ValueError/TypeError: a malformed pk (a bad UUID) is a
        # miss, not a 500.
        raise Http404(f"No {model.__name__} matches the given query.") from exc
