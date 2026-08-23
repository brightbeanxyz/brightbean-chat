"""Flow routes, mounted at ``/w/<uuid:workspace_id>/``.

The mount point is the workspace prefix rather than ``flows/`` so that both
halves of this app can live under one namespace: the pages sit at
``/w/<id>/flows/…`` and the builder data API at ``/w/<id>/api/flows/…``, which
is SPEC §16's path with the workspace segment RBACMiddleware requires prepended
(see :mod:`apps.flows.api` for why that deviation is deliberate).
"""

from django.urls import path

from apps.flows import api, views, views_triggers

app_name = "flows"

urlpatterns = [
    path("flows/", views.flow_list, name="list"),
    path("flows/create/", views.flow_create, name="create"),
    path("flows/<uuid:flow_id>/edit/", views.flow_edit, name="edit"),
    path("flows/<uuid:flow_id>/rename/", views.flow_rename, name="rename"),
    path("flows/<uuid:flow_id>/duplicate/", views.flow_duplicate, name="duplicate"),
    path("flows/<uuid:flow_id>/archive/", views.flow_archive, name="archive"),
    path("flows/<uuid:flow_id>/restore/", views.flow_restore, name="restore"),
    # The Triggers panel (issue #11). Partials and one image, all under the
    # existing flow page, so `flows:edit` stays the route the nav lights up for.
    path("flows/<uuid:flow_id>/triggers/", views_triggers.trigger_panel, name="trigger_panel"),
    path("flows/<uuid:flow_id>/triggers/form/", views_triggers.trigger_form, name="trigger_form"),
    path("flows/<uuid:flow_id>/triggers/create/", views_triggers.trigger_create, name="trigger_create"),
    path("flows/<uuid:flow_id>/triggers/<uuid:trigger_id>/", views_triggers.trigger_update, name="trigger_update"),
    path(
        "flows/<uuid:flow_id>/triggers/<uuid:trigger_id>/toggle/", views_triggers.trigger_toggle, name="trigger_toggle"
    ),
    path("flows/<uuid:flow_id>/triggers/<uuid:trigger_id>/move/", views_triggers.trigger_move, name="trigger_move"),
    path(
        "flows/<uuid:flow_id>/triggers/<uuid:trigger_id>/delete/", views_triggers.trigger_delete, name="trigger_delete"
    ),
    # The comment trigger's post picker (#17/#18). Bound to a connection because
    # posts belong to an account, not to a flow; both ids are tenant-scoped and
    # tests/idor.py already knows how to build them.
    path(
        "flows/<uuid:flow_id>/triggers/posts/<uuid:connection_id>/",
        views_triggers.trigger_posts,
        name="trigger_posts",
    ),
    path(
        "flows/<uuid:flow_id>/triggers/<uuid:trigger_id>/qr/<uuid:connection_id>/",
        views_triggers.trigger_qr,
        name="trigger_qr",
    ),
    # SPEC §16's data API. `schema/` is listed before the uuid route for
    # readability only — the converter would never match it either way.
    path("api/flows/schema/", api.flow_schema, name="api_schema"),
    path("api/flows/<uuid:flow_id>/", api.flow_detail, name="api_detail"),
    path("api/flows/<uuid:flow_id>/publish/", api.flow_publish, name="api_publish"),
    path("api/flows/<uuid:flow_id>/stats/", api.flow_stats, name="api_stats"),
]
