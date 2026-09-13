"""The two billing POST routes.

``app_name`` is set here, unlike ``urls_webhooks.py``: these are reversed as
``billing:checkout`` and ``billing:portal`` from a template that this app owns
the behaviour of, so there is no flat name reserved elsewhere to preserve.

Mounted at ``organization/billing/`` **before** the ``organization/`` include, so
the deeper prefix is tried first — the convention ``config/urls.py`` already
states for the workspace mounts. The billing *page* itself stays at
``organizations:billing``; only these two writes live here.
"""

from django.urls import path

from apps.billing import views

app_name = "billing"

urlpatterns = [
    path("checkout/", views.checkout, name="checkout"),
    path("portal/", views.portal, name="portal"),
]
