"""Org-scoped routes, mounted at ``/organization/``.

Deviation 1 fixes ``/w/<uuid:workspace_id>/`` for *workspace-scoped* routes.
These are org-scoped. Note ``target_id`` rather than ``workspace_id`` on the
archive route — see the view's docstring.
"""

from django.urls import path

from apps.organizations import views

app_name = "organizations"

urlpatterns = [
    path("settings/", views.settings_view, name="settings"),
    path("settings/update/", views.update_settings, name="update_settings"),
    path("workspaces/", views.workspaces_view, name="workspaces"),
    path("workspaces/create/", views.create_workspace, name="create_workspace"),
    path("workspaces/<uuid:target_id>/archived/", views.set_workspace_archived, name="set_workspace_archived"),
]
