"""Contacts routes, mounted at ``/w/<uuid:workspace_id>/``.

This app owns two disjoint stretches of the workspace URL space — the
``contacts/`` subtree, which is the CRM (issue #13), and the ``settings/tags/``
+ ``settings/fields/`` pair. One module rather than two mounts, because
``config/urls.py`` is contended by five parallel workstreams and this app should
add exactly one line to it.

Mounted after ``apps.workspaces.urls``, so these patterns can only be shadowed,
never shadow: Django tries each include in order and falls through when nothing
inside matches.

**Literal routes come before ``contacts/<uuid:contact_id>/``.** In practice
``<uuid:…>`` cannot match ``rows`` or ``import``, so the order is not what makes
this correct — but a later route named with a converter that *is* permissive
would silently be shadowed by the detail page, and reading the file top to bottom
should not depend on knowing which converters are strict.

Every ``<uuid:…>`` kwarg here is swept by ``tests/idor.py``, which needs a
resolver registered for each one: ``contact_id``, ``segment_id``, ``identity_id``
and ``import_id`` joined ``tag_id`` and ``field_id`` there for this issue. What
the sweep cannot see is the tenant-shaped ids this app also accepts in the
**query string and the POST body** — ``?segment=``, ``?filter=`` and the bulk
endpoints' ``ids`` — so those have their own cross-tenant tests.
"""

from django.urls import path

from apps.contacts import views

app_name = "contacts"

urlpatterns = [
    # --- the CRM ----------------------------------------------------------
    path("contacts/", views.contact_list, name="list"),
    path("contacts/rows/", views.contact_rows, name="rows"),
    path("contacts/export/", views.contact_export, name="export"),
    path("contacts/bulk/tag/", views.bulk_tag, name="bulk_tag"),
    path("contacts/bulk/delete/", views.bulk_delete, name="bulk_delete"),
    path("contacts/bulk/sequence/", views.bulk_sequence, name="bulk_sequence"),
    path("contacts/segments/create/", views.segment_create, name="segment_create"),
    path("contacts/segments/<uuid:segment_id>/save/", views.segment_update, name="segment_update"),
    path("contacts/segments/<uuid:segment_id>/delete/", views.segment_delete, name="segment_delete"),
    # --- CSV import -------------------------------------------------------
    path("contacts/import/", views.import_list, name="import_list"),
    path("contacts/import/upload/", views.import_upload, name="import_upload"),
    path("contacts/import/<uuid:import_id>/", views.import_detail, name="import_detail"),
    path("contacts/import/<uuid:import_id>/progress/", views.import_progress, name="import_progress"),
    path("contacts/import/<uuid:import_id>/mapping/", views.import_mapping, name="import_mapping"),
    path("contacts/import/<uuid:import_id>/run/", views.import_run, name="import_run"),
    path("contacts/import/<uuid:import_id>/report/", views.import_report, name="import_report"),
    # --- one contact ------------------------------------------------------
    path("contacts/<uuid:contact_id>/", views.contact_detail, name="detail"),
    path("contacts/<uuid:contact_id>/activity/", views.contact_activity, name="activity"),
    path("contacts/<uuid:contact_id>/channels/", views.contact_channels, name="channels"),
    path("contacts/<uuid:contact_id>/tags/", views.contact_tags, name="tags"),
    path("contacts/<uuid:contact_id>/tags/add/", views.contact_tag_add, name="tag_add"),
    path("contacts/<uuid:contact_id>/tags/suggest/", views.tag_suggestions, name="tag_suggest"),
    path("contacts/<uuid:contact_id>/tags/<uuid:tag_id>/remove/", views.contact_tag_remove, name="tag_remove"),
    path("contacts/<uuid:contact_id>/edit/", views.contact_edit, name="edit"),
    path("contacts/<uuid:contact_id>/fields/<uuid:field_id>/", views.contact_field_value, name="field_value"),
    path(
        "contacts/<uuid:contact_id>/identities/<uuid:identity_id>/opt-out/",
        views.identity_opt_out,
        name="identity_opt_out",
    ),
    path("contacts/<uuid:contact_id>/start-flow/", views.contact_start_flow, name="start_flow"),
    path("contacts/<uuid:contact_id>/stop-automation/", views.contact_stop_automation, name="stop_automation"),
    path("contacts/<uuid:contact_id>/delete/", views.contact_delete, name="delete"),
    # --- workspace settings ----------------------------------------------
    path("settings/tags/", views.tag_list, name="tag_list"),
    path("settings/tags/rows/", views.tag_rows, name="tag_rows"),
    path("settings/tags/create/", views.tag_create, name="tag_create"),
    path("settings/tags/<uuid:tag_id>/rename/", views.tag_rename, name="tag_rename"),
    path("settings/tags/<uuid:tag_id>/merge/", views.tag_merge, name="tag_merge"),
    path("settings/tags/<uuid:tag_id>/delete/", views.tag_delete, name="tag_delete"),
    path("settings/fields/", views.field_list, name="field_list"),
    path("settings/fields/rows/", views.field_rows, name="field_rows"),
    path("settings/fields/create/", views.field_create, name="field_create"),
    path("settings/fields/<uuid:field_id>/rename/", views.field_rename, name="field_rename"),
    path("settings/fields/<uuid:field_id>/delete/", views.field_delete, name="field_delete"),
]
