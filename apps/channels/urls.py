"""Connection management, mounted at ``/w/<uuid:workspace_id>/settings/channels/``.

The public webhook routes live in ``urls_webhooks.py`` and are mounted
separately at ``/webhooks/`` — they are unauthenticated and must never sit under
a workspace prefix, where ``RBACMiddleware`` would try to resolve a membership
for a platform's POST.
"""

from django.urls import path

from apps.channels import views

app_name = "channels"

urlpatterns = [
    path("", views.connection_list, name="list"),
    path("new/", views.connection_create, name="create"),
    path("<uuid:connection_id>/", views.connection_detail, name="detail"),
    path("<uuid:connection_id>/status/", views.connection_set_status, name="set_status"),
    path("<uuid:connection_id>/rotate-secret/", views.connection_rotate_secret, name="rotate_secret"),
    path("<uuid:connection_id>/delete/", views.connection_delete, name="delete"),
]
