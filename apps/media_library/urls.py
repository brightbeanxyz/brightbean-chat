"""Mounted at ``/w/<uuid:workspace_id>/media/`` (SPEC §16).

The kwarg name ``workspace_id`` is ``RBACMiddleware``'s resolution contract; a
route that spells it differently silently loses the membership check.
"""

from django.urls import path

from apps.media_library import views

app_name = "media"

urlpatterns = [
    path("", views.library, name="library"),
    path("upload/", views.upload, name="upload"),
    path("picker/", views.picker, name="picker"),
    path("folders/create/", views.folder_create, name="folder_create"),
    path("folders/<uuid:folder_id>/rename/", views.folder_rename, name="folder_rename"),
    path("folders/<uuid:folder_id>/delete/", views.folder_delete, name="folder_delete"),
    path("<uuid:asset_id>/", views.asset_detail, name="asset_detail"),
    path("<uuid:asset_id>/edit/", views.asset_edit, name="asset_edit"),
    path("<uuid:asset_id>/move/", views.asset_move, name="asset_move"),
    path("<uuid:asset_id>/delete/", views.asset_delete, name="asset_delete"),
]
