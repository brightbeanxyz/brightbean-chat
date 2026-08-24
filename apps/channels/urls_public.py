"""The unauthenticated unsubscribe route, mounted at the site root.

``/u/<token>/`` is the ``/u/`` entry in the public token-route family that
``apps/common/signing.py`` documents — alongside ``/m/`` media delivery and
``/c/`` click tracking and ``/o/`` open pixels, which #26 landed in
``apps/analytics/urls_public.py``.

Short on purpose, and not under ``/w/<workspace_id>/``: this URL is embedded in
every outbound email, it is opened by a recipient who has no account here, and
``RBACMiddleware`` would try to resolve a membership for them.
"""

from django.urls import path

from apps.channels import views_unsubscribe

urlpatterns = [
    path("u/<str:token>/", views_unsubscribe.unsubscribe, name="unsubscribe"),
]
