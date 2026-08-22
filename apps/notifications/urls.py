"""Mounted at ``/notifications/``.

Not under ``/w/<uuid:workspace_id>/``: a notification is addressed to a person,
not to a workspace, and the bell deliberately shows every workspace at once — a
``channel_needs_reauth`` alert for the workspace you are *not* looking at is
precisely the one you need to see.
"""

from django.urls import path

from apps.notifications import views

app_name = "notifications"

urlpatterns = [
    path("", views.notification_list, name="list"),
    path("bell/", views.bell_panel, name="bell"),
    path("badge/", views.unread_badge, name="badge"),
    path("read-all/", views.mark_all_read, name="mark_all_read"),
    path("<uuid:notification_id>/read/", views.mark_read, name="mark_read"),
    path("email-preference/", views.update_email_preference, name="email_preference"),
]
