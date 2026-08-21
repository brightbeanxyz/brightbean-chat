"""The unauthenticated delivery route, mounted at the site root.

``/m/<token>/`` joins the public token-route family documented in
``apps/common/signing.py`` — ``/u/`` unsubscribe, ``/c/`` click tracking,
``/o/`` open pixels. Short on purpose: this URL is embedded in outbound messages
and, for some platforms, counts against a character budget.
"""

from django.urls import path

from apps.media_library import views

urlpatterns = [
    path("m/<str:token>/", views.deliver, name="media_delivery"),
]
