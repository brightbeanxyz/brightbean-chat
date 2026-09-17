"""The Stripe webhook route.

Deliberately **no** ``app_name``, so the route keeps the flat name
``webhook_stripe`` that sits beside ``webhook_sms`` and ``webhook_email`` in the
URL namespace — the arrangement ``apps/api/urls_keys.py`` documents for
``settings_org_api_keys``. Every route in this project is named, because
``tests/idor.py`` reverses by name and raises rather than skipping an unnamed
one.

It is mounted in ``config/urls.py`` **before** the channels webhook include, and
that ordering is load-bearing rather than tidy. See the comment there.
"""

from django.urls import path

from apps.billing import views_webhooks

urlpatterns = [
    path("", views_webhooks.stripe_webhook, name="webhook_stripe"),
]
