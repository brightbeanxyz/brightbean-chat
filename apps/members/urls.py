"""Member management, mounted at ``/organization/members/``."""

from django.urls import path

from apps.members import views

app_name = "members"

urlpatterns = [
    path("", views.member_list, name="list"),
    path("invite/", views.invite_member, name="invite"),
    path("invite/<uuid:invitation_id>/resend/", views.resend_invite, name="resend_invite"),
    path("invite/<uuid:invitation_id>/revoke/", views.revoke_invite, name="revoke_invite"),
    path("<uuid:membership_id>/role/", views.update_member_role, name="update_role"),
    path("<uuid:membership_id>/remove/", views.remove_member_view, name="remove"),
    path("<uuid:membership_id>/workspaces/", views.manage_workspaces, name="manage_workspaces"),
]
