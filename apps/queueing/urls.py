"""The internal tick route.

Deliberately not under ``/w/<uuid:workspace_id>/``: this is a deployment-level
operations endpoint authenticated by a shared token, not a tenant page. It
carries no tenant-identifying kwarg, so ``tests/idor.py`` skips it correctly
(and it is named, so the suite's unnamed-route guard is satisfied).
"""

from django.urls import path

from apps.queueing import views

urlpatterns = [
    # No trailing slash: SPEC §15 writes /internal/tick, external pingers hit
    # literal URLs, and /healthz sets the same precedent.
    path("internal/tick", views.internal_tick, name="internal_tick"),
]
