"""Sequence routes, mounted at ``/w/<uuid:workspace_id>/sequences/`` (SPEC §16).

The kwarg name ``workspace_id`` is ``RBACMiddleware``'s entire resolution
contract; a route that spells it differently silently loses the membership check
and the 404.

Every route is named, because ``tests/idor.py`` reverses by name and refuses to
skip a tenant route that has none — and every route below the list carries a
``sequence_id``, which is the definition of one the sweep has to reach.
"""

from django.urls import path

from apps.campaigns import views

app_name = "campaigns"

urlpatterns = [
    path("", views.sequence_list, name="list"),
    path("create/", views.sequence_create, name="create"),
    path("<uuid:sequence_id>/", views.sequence_detail, name="detail"),
    path("<uuid:sequence_id>/rename/", views.sequence_rename, name="rename"),
    path("<uuid:sequence_id>/status/", views.sequence_status, name="status"),
    path("<uuid:sequence_id>/delete/", views.sequence_delete, name="delete"),
    path("<uuid:sequence_id>/steps/", views.steps_panel, name="steps"),
    path("<uuid:sequence_id>/steps/create/", views.step_create, name="step_create"),
    path("<uuid:sequence_id>/steps/<uuid:step_id>/", views.step_update, name="step_update"),
    path("<uuid:sequence_id>/steps/<uuid:step_id>/move/", views.step_move, name="step_move"),
    path("<uuid:sequence_id>/steps/<uuid:step_id>/delete/", views.step_delete, name="step_delete"),
    path("<uuid:sequence_id>/subscribers/", views.subscribers_panel, name="subscribers"),
    path("<uuid:sequence_id>/subscribers/add/", views.subscriber_add, name="subscriber_add"),
    path(
        "<uuid:sequence_id>/subscribers/<uuid:enrollment_id>/remove/",
        views.subscriber_remove,
        name="subscriber_remove",
    ),
]
