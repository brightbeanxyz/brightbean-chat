"""Connection management, mounted at ``/w/<uuid:workspace_id>/settings/channels/``.

The public webhook routes live in ``urls_webhooks.py`` and are mounted
separately at ``/webhooks/`` — they are unauthenticated and must never sit under
a workspace prefix, where ``RBACMiddleware`` would try to resolve a membership
for a platform's POST.
"""

from django.urls import path

from apps.channels import views, views_instagram, views_sms, views_telegram, views_whatsapp

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
    # WhatsApp's guided connect flow and its template manager (issue #19).
    # Declared before the ``<uuid:connection_id>`` routes for readability only —
    # the converter would never match "whatsapp".
    path("whatsapp/connect/", views_whatsapp.whatsapp_connect, name="whatsapp_connect"),
    path("whatsapp/templates/", views_whatsapp.whatsapp_templates_list, name="whatsapp_templates"),
    path("whatsapp/templates/new/", views_whatsapp.whatsapp_template_new, name="whatsapp_template_new"),
    # No template id: the preview renders what is currently typed into the form,
    # which exists before anything has been saved.
    path(
        "whatsapp/templates/preview/",
        views_whatsapp.whatsapp_template_preview,
        name="whatsapp_template_preview",
    ),
    path(
        "whatsapp/templates/<uuid:template_id>/",
        views_whatsapp.whatsapp_template_edit,
        name="whatsapp_template_edit",
    ),
    path(
        "whatsapp/templates/<uuid:template_id>/submit/",
        views_whatsapp.whatsapp_template_submit,
        name="whatsapp_template_submit",
    ),
    path(
        "whatsapp/templates/<uuid:template_id>/delete/",
        views_whatsapp.whatsapp_template_delete,
        name="whatsapp_template_delete",
    ),
    path("whatsapp/pricing/", views_whatsapp.whatsapp_cost_hints, name="whatsapp_cost_hints"),
    # Instagram's guided connect (issue #17). The OAuth *callback* is not here:
    # Meta allows one exact redirect URI per app, so it cannot carry a workspace
    # id and lives in ``urls_oauth.py`` at the deployment root instead.
    path("instagram/connect/", views_instagram.instagram_connect, name="instagram_connect"),
    path("instagram/posts/", views_instagram.instagram_posts, name="instagram_posts"),
    # Twilio's guided connect flow, SPEC §6.6's configurable compliance copy,
    # and the segment-count preview the send_sms panel and L6-B's composer
    # both call (issue #20). Declared before the ``<uuid:connection_id>``
    # routes for readability only — the converter would never match "sms".
    path("sms/connect/", views_sms.sms_connect, name="sms_connect"),
    path("sms/settings/", views_sms.sms_settings, name="sms_settings"),
    path("sms/settings/update/", views_sms.sms_settings_update, name="sms_settings_update"),
    path("sms/segments/", views_sms.sms_segment_preview, name="sms_segment_preview"),
    path("<uuid:connection_id>/", views.connection_detail, name="detail"),
    path("<uuid:connection_id>/status/", views.connection_set_status, name="set_status"),
    path("<uuid:connection_id>/rotate-secret/", views.connection_rotate_secret, name="rotate_secret"),
    path("<uuid:connection_id>/delete/", views.connection_delete, name="delete"),
]
