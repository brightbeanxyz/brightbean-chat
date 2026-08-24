"""Analytics routes, mounted at ``/w/<uuid:workspace_id>/analytics/`` (SPEC §16).

The kwarg name ``workspace_id`` is ``RBACMiddleware``'s entire resolution
contract; a route that spells it differently silently loses the membership check
and the 404.

Every route is named. ``tests/idor.py`` reverses by name and raises rather than
skipping a tenant route that has none — and ``flow_id`` already has a resolver
there, so the flow page joins the sweep with no new registration.

The two *public* tracking routes are not here: they carry no workspace and are
mounted at the site root by :mod:`apps.analytics.urls_public`.
"""

from django.urls import path

from apps.analytics import views

app_name = "analytics"

urlpatterns = [
    path("", views.overview, name="overview"),
    path("flows/<uuid:flow_id>/", views.flow_detail, name="flow_detail"),
    path("settings/", views.tracking_settings, name="tracking_settings"),
    path("settings/update/", views.update_tracking_settings, name="update_tracking_settings"),
]
