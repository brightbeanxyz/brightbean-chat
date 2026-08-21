"""Contacts routes, mounted at ``/w/<uuid:workspace_id>/``.

This app owns two disjoint stretches of the workspace URL space — the
``contacts/`` subtree, which issue #13 extends with detail and import routes,
and the ``settings/tags/`` + ``settings/fields/`` pair. One module rather than
two mounts, because ``config/urls.py`` is contended by five parallel
workstreams and this issue should add exactly one line to it.

Mounted after ``apps.workspaces.urls``, so these patterns can only be shadowed,
never shadow: Django tries each include in order and falls through when nothing
inside matches.
"""

from django.urls import path

from apps.contacts import views

app_name = "contacts"

urlpatterns = [
    path("contacts/", views.contact_list, name="list"),
    path("settings/tags/", views.tag_list, name="tag_list"),
    path("settings/tags/rows/", views.tag_rows, name="tag_rows"),
    path("settings/tags/create/", views.tag_create, name="tag_create"),
    path("settings/tags/<uuid:tag_id>/rename/", views.tag_rename, name="tag_rename"),
    path("settings/tags/<uuid:tag_id>/delete/", views.tag_delete, name="tag_delete"),
    path("settings/fields/", views.field_list, name="field_list"),
    path("settings/fields/rows/", views.field_rows, name="field_rows"),
    path("settings/fields/create/", views.field_create, name="field_create"),
    path("settings/fields/<uuid:field_id>/rename/", views.field_rename, name="field_rename"),
    path("settings/fields/<uuid:field_id>/delete/", views.field_delete, name="field_delete"),
]
