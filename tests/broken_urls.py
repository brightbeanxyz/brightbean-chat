"""A deliberately leaky URLconf, used to prove the IDOR suite actually catches
something (brief, deviation 3).

The bug is a realistic one rather than a strawman: the view takes the tenant id
under a name that is *not* ``workspace_id``, so ``RBACMiddleware`` — whose whole
resolution contract is ``view_kwargs.get("workspace_id")`` — never sees it, and
the view then queries with ``.unscoped()``. Both halves are things a developer
does for defensible-sounding reasons, and together they cross tenants with a
200.

Never referenced by ``config/urls.py``; a test points ``ROOT_URLCONF`` here.
"""

from django.http import HttpRequest, HttpResponse
from django.urls import path

from apps.credentials.models import WorkspaceCredentialOverride


def leaky_view(request: HttpRequest, target_id: str) -> HttpResponse:
    # unscoped() with no justification — exactly what CONTRIBUTING.md forbids.
    count = WorkspaceCredentialOverride.objects.unscoped().filter(workspace_id=target_id).count()
    return HttpResponse(f"{count} credential overrides")


urlpatterns = [
    path("leaky/<uuid:target_id>/", leaky_view, name="leaky"),
]
