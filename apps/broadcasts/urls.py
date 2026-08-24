"""Broadcast routes, mounted at ``/w/<uuid:workspace_id>/broadcasts/`` (SPEC §16).

The kwarg name ``workspace_id`` is ``RBACMiddleware``'s entire resolution
contract; a route that spells it differently silently loses the membership check
and the 404.

Every route is named. ``tests/idor.py`` reverses by name and raises
``UnnamedTenantRouteError`` rather than skipping a tenant route that has none —
and it also raises for a kwarg it cannot build, which is why ``broadcast_id`` has
a resolver registered there.
"""

from django.urls import path

from apps.broadcasts import views

app_name = "broadcasts"

urlpatterns = [
    path("", views.broadcast_list, name="list"),
    path("rows/", views.broadcast_rows, name="rows"),
    path("new/", views.broadcast_create, name="create"),
    path("<uuid:broadcast_id>/", views.broadcast_detail, name="detail"),
    path("<uuid:broadcast_id>/counters/", views.counters, name="counters"),
    path("<uuid:broadcast_id>/recipients/", views.recipients, name="recipients"),
    path("<uuid:broadcast_id>/compose/", views.compose, name="compose"),
    path("<uuid:broadcast_id>/compose/step/", views.wizard, name="wizard"),
    path("<uuid:broadcast_id>/compose/channel/", views.save_channel, name="save_channel"),
    path("<uuid:broadcast_id>/compose/audience/", views.save_audience, name="save_audience"),
    path("<uuid:broadcast_id>/compose/audience/preview/", views.audience_preview, name="audience_preview"),
    path("<uuid:broadcast_id>/compose/content/", views.save_content, name="save_content"),
    path("<uuid:broadcast_id>/compose/schedule/", views.save_schedule, name="save_schedule"),
    path("<uuid:broadcast_id>/cancel/", views.broadcast_cancel, name="cancel"),
    path("<uuid:broadcast_id>/duplicate/", views.broadcast_duplicate, name="duplicate"),
    path("<uuid:broadcast_id>/delete/", views.broadcast_delete, name="delete"),
]
