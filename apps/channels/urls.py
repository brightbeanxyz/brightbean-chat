"""Connection management, mounted at ``/w/<uuid:workspace_id>/settings/channels/``.

The public webhook routes live in ``urls_webhooks.py`` and are mounted
separately at ``/webhooks/`` — they are unauthenticated and must never sit under
a workspace prefix, where ``RBACMiddleware`` would try to resolve a membership
for a platform's POST.
"""

from django.urls import path

from apps.channels import views, views_messenger, views_telegram

app_name = "channels"

urlpatterns = [
    path("", views.connection_list, name="list"),
    path("new/", views.connection_create, name="create"),
    # Telegram's guided connect flow (issue #12) and the flow builder's
    # "test on Telegram" link. Declared before the ``<uuid:connection_id>``
    # routes for readability only — the converter would never match "telegram".
    path("telegram/connect/", views_telegram.telegram_connect, name="telegram_connect"),
    path(
        "telegram/preview/<uuid:flow_id>/",
        views_telegram.telegram_preview,
        name="telegram_preview",
    ),
    # Messenger's guided connect (issue #18). The OAuth *callback* is not here:
    # Meta whitelists one exact redirect URI per app, so it cannot carry a
    # workspace id and lives at /oauth/meta/callback/ instead — see
    # apps/channels/urls_oauth.py.
    path("messenger/connect/", views_messenger.messenger_connect, name="messenger_connect"),
    path("messenger/pages/", views_messenger.messenger_pages, name="messenger_pages"),
    path("<uuid:connection_id>/", views.connection_detail, name="detail"),
    path("<uuid:connection_id>/status/", views.connection_set_status, name="set_status"),
    path("<uuid:connection_id>/rotate-secret/", views.connection_rotate_secret, name="rotate_secret"),
    path("<uuid:connection_id>/delete/", views.connection_delete, name="delete"),
]
