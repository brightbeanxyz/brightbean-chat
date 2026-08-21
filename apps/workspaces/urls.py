"""Workspace-scoped routes, mounted at ``/w/<uuid:workspace_id>/``.

``docs/SPEC.md`` §16 specifies the ``/w/`` prefix. Studio uses
``workspace/<uuid>/`` for scoped apps but ``workspaces/<uuid>/settings/`` for
management, which is two conventions for one concept; this is one (deviation 1).
The kwarg stays ``workspace_id`` because that is what ``RBACMiddleware`` reads.
No slugs — a slug would be a second, mutable identifier for a tenant boundary.
"""

from django.urls import path

from apps.workspaces import views

app_name = "workspaces"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("switch/", views.switch, name="switch"),
    path("settings/", views.settings_view, name="settings"),
    path("settings/update/", views.update_settings, name="update_settings"),
]
