"""Public webhook routes, mounted at ``/webhooks/`` (SPEC §7.1).

Unauthenticated and not workspace-scoped: these URLs are handed to a platform's
console, and the signature is the credential. That is also why they are **not**
under ``/w/<workspace_id>/`` — the platform posting an event has no session and
no idea which workspace it is talking to.

Route order matters. ``sms/`` and ``email/`` are declared before
``<str:platform>/`` so the specific shapes win; a bare ``/webhooks/sms/`` still
falls through to the shared route, where the SMS adapter finds no connection to
resolve and the request gets the same 403 as any other unidentifiable delivery.

Every route is named, which ``tests/idor.py`` requires — and the two carrying a
``connection_id`` are waived there, with the reason, because an unauthenticated
endpoint has no session tenant to compare against and deliberately answers an
identical 403 for an unknown connection and a bad signature.
"""

from django.urls import path

from apps.channels import views_webhooks

urlpatterns = [
    path("sms/<uuid:connection_id>/", views_webhooks.sms_webhook, name="webhook_sms"),
    path(
        "email/<str:provider>/<uuid:connection_id>/",
        views_webhooks.email_webhook,
        name="webhook_email",
    ),
    path("<str:platform>/", views_webhooks.platform_webhook, name="webhook_platform"),
]
