"""The two unauthenticated tracking routes, mounted at the site root.

``/c/<token>/`` and ``/o/<token>/`` are the entries ``apps/common/signing.py``
reserves for issue #26, alongside ``/u/`` unsubscribe and ``/m/`` media delivery.

Short and not under ``/w/<workspace_id>/``, for the reason
``apps/channels/urls_public.py`` gives about its own route: these URLs are
embedded in messages, they are opened by a recipient who has no account here, and
``RBACMiddleware`` would try to resolve a membership for them. The ``/c/`` one is
also charged by the character on SMS.

Both are named. ``tests/idor.py`` reverses by name, and both names appear in its
``WAIVED_ROUTES`` with the position on why a public token route cannot answer the
sweep's question.
"""

from django.urls import path

from apps.analytics import views_public

urlpatterns = [
    path("c/<str:token>/", views_public.click_redirect, name="click_redirect"),
    path("o/<str:token>/", views_public.open_pixel, name="open_pixel"),
]
