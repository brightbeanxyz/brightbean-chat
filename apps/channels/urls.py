"""Connection management, mounted at ``/w/<uuid:workspace_id>/settings/channels/``.

The public webhook routes live in ``urls_webhooks.py`` and are mounted
separately at ``/webhooks/`` — they are unauthenticated and must never sit under
a workspace prefix, where ``RBACMiddleware`` would try to resolve a membership
for a platform's POST.
"""

from django.urls import path

from apps.channels import views, views_email, views_telegram

app_name = "channels"

urlpatterns = [
    path("", views.connection_list, name="list"),
    path("new/", views.connection_create, name="create"),
    # The guided connect flows (issues #12 and #21) and the flow builder's
    # "test on Telegram" link. Declared before the ``<uuid:connection_id>``
    # routes for readability only — the converter would never match a word.
    path("email/connect/", views_email.email_connect, name="email_connect"),
    path("telegram/connect/", views_telegram.telegram_connect, name="telegram_connect"),
    path(
        "telegram/preview/<uuid:flow_id>/",
        views_telegram.telegram_preview,
        name="telegram_preview",
    ),
    path("<uuid:connection_id>/", views.connection_detail, name="detail"),
    path("<uuid:connection_id>/status/", views.connection_set_status, name="set_status"),
    path("<uuid:connection_id>/rotate-secret/", views.connection_rotate_secret, name="rotate_secret"),
    path("<uuid:connection_id>/delete/", views.connection_delete, name="delete"),
    path("<uuid:connection_id>/test-email/", views_email.send_test_email, name="send_test_email"),
]
