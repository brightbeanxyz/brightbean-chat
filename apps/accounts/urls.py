"""Routes mounted at ``/accounts/`` **before** ``allauth.urls``.

Order matters in ``config/urls.py``: this include comes first, so
``/accounts/signup/`` resolves to the invite-aware view rather than allauth's.
Reversing ``account_signup`` (unnamespaced, which is what allauth's own
templates do) still yields ``/accounts/signup/`` and therefore still lands here.
"""

from django.urls import path

from apps.accounts import views
from apps.accounts.views_signup import InvitePrefillSignupView

app_name = "accounts"

urlpatterns = [
    path("signup/", InvitePrefillSignupView.as_view(), name="account_signup"),
    path("settings/", views.account_settings, name="settings"),
]
