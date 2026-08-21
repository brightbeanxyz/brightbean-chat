"""Mounted at ``/w/<uuid:workspace_id>/settings/credentials/``."""

from django.urls import path

from apps.credentials import views

app_name = "credentials"

urlpatterns = [
    path("", views.credential_list, name="list"),
    path("<str:platform>/", views.edit_override, name="edit"),
    path("<str:platform>/clear/", views.clear_override, name="clear"),
]
