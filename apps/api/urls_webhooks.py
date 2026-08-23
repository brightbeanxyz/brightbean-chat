"""Outbound webhooks, mounted at ``/w/<uuid:workspace_id>/settings/webhooks/``."""

from django.urls import path

from apps.api import views_webhooks

app_name = "api_webhooks"

urlpatterns = [
    path("", views_webhooks.webhook_list, name="list"),
    path("create/", views_webhooks.webhook_create, name="create"),
    path("<uuid:webhook_id>/", views_webhooks.webhook_detail, name="detail"),
    path("<uuid:webhook_id>/update/", views_webhooks.webhook_update, name="update"),
    path("<uuid:webhook_id>/rotate-secret/", views_webhooks.webhook_rotate_secret, name="rotate_secret"),
    path("<uuid:webhook_id>/test/", views_webhooks.webhook_test, name="test"),
    path("<uuid:webhook_id>/delete/", views_webhooks.webhook_delete, name="delete"),
]
