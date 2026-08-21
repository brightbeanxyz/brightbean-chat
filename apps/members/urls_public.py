"""The one members route that is not under ``/organization/``.

Invite acceptance is unauthenticated — the recipient has no organization yet, so
mounting it under an org-scoped prefix would be a lie about who can reach it.
"""

from django.urls import path

from apps.members import views

urlpatterns = [
    path("invite/<str:token>/", views.accept_invite, name="accept_invite"),
]
