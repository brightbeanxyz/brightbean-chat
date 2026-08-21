"""Signup provisioning (deviation 8).

Studio provisions from a ``post_save`` on every ``User`` row *and* from allauth's
``user_signed_up``, so an invited signup has to delete the default organization
that the first receiver just created — matched by the literal name
"My Organization". Here ``post_save`` is gone.
"""

import pytest
from django.urls import reverse

from apps.accounts.services import ensure_provisioned, provision_organization_and_workspace
from apps.members.models import Invitation, OrgMembership, WorkspaceMembership
from apps.members.roles import OrgRole, WorkspaceRole
from apps.members.services import create_invitation
from apps.members.signals_keys import PENDING_INVITE_SESSION_KEY
from apps.organizations.models import Organization
from tests.support import TEST_PASSWORD, create_user


@pytest.mark.django_db
class TestCreatingAUserCreatesNothingElse:
    def test_no_organization_is_provisioned_on_create_user(self):
        """The reason Studio's three test modules each carry a teardown helper."""
        user = create_user("bare@example.test")

        assert not OrgMembership.objects.filter(user=user).exists()
        assert Organization.objects.count() == 0

    def test_the_primitive_is_idempotent(self):
        user = create_user("bare@example.test")

        first = provision_organization_and_workspace(user)
        second = provision_organization_and_workspace(user)

        assert first == second
        assert OrgMembership.objects.filter(user=user).count() == 1


@pytest.mark.django_db
class TestSignupProvisioning:
    def test_signing_up_creates_an_org_and_a_workspace(self, client):
        response = client.post(
            reverse("account_signup"),
            {"email": "founder@example.test", "password1": TEST_PASSWORD},
            follow=True,
        )

        assert response.status_code == 200
        membership = OrgMembership.objects.get(user__email="founder@example.test")
        assert membership.org_role == OrgRole.OWNER
        workspace_membership = WorkspaceMembership.objects.get(user=membership.user)
        assert workspace_membership.workspace_role == WorkspaceRole.ADMIN
        assert membership.user.last_workspace_id == workspace_membership.workspace_id

    def test_signup_lands_on_the_workspace_dashboard(self, client):
        response = client.post(
            reverse("account_signup"),
            {"email": "founder@example.test", "password1": TEST_PASSWORD},
            follow=True,
        )

        workspace = WorkspaceMembership.objects.get(user__email="founder@example.test").workspace
        assert response.redirect_chain[-1][0] == f"/w/{workspace.pk}/"


@pytest.mark.django_db
class TestInvitedSignup:
    def _pending_invite(self, tenancy) -> Invitation:
        return create_invitation(
            org=tenancy.organization,
            email="invitee@example.test",
            org_role=OrgRole.MEMBER,
            workspace_assignments=[{"workspace_id": str(tenancy.workspace.pk), "role": WorkspaceRole.EDITOR.value}],
            invited_by=tenancy.owner,
        )

    def test_an_invited_signup_joins_the_inviting_org(self, tenancy, client):
        invitation = self._pending_invite(tenancy)
        client.get(f"/invite/{invitation.token}/")

        client.post(
            reverse("account_signup"),
            {"email": "invitee@example.test", "password1": TEST_PASSWORD},
            follow=True,
        )

        memberships = OrgMembership.objects.filter(user__email="invitee@example.test")
        assert [m.organization_id for m in memberships] == [tenancy.organization.pk]

    def test_no_default_organization_is_created_and_deleted(self, tenancy, client):
        """Studio creates one and then removes it by name-matching."""
        invitation = self._pending_invite(tenancy)
        client.get(f"/invite/{invitation.token}/")
        before = Organization.objects.count()

        client.post(
            reverse("account_signup"),
            {"email": "invitee@example.test", "password1": TEST_PASSWORD},
            follow=True,
        )

        assert Organization.objects.count() == before

    def test_the_invited_workspace_role_is_applied(self, tenancy, client):
        invitation = self._pending_invite(tenancy)
        client.get(f"/invite/{invitation.token}/")

        client.post(
            reverse("account_signup"),
            {"email": "invitee@example.test", "password1": TEST_PASSWORD},
            follow=True,
        )

        membership = WorkspaceMembership.objects.get(user__email="invitee@example.test")
        assert membership.workspace_role == WorkspaceRole.EDITOR

    def test_the_signup_form_prefills_the_invited_address(self, tenancy, client):
        invitation = self._pending_invite(tenancy)
        client.get(f"/invite/{invitation.token}/")

        response = client.get(reverse("account_signup"))

        assert b"invitee@example.test" in response.content
        assert b"readonly" in response.content

    def test_the_token_is_stashed_in_the_session(self, tenancy, client):
        invitation = self._pending_invite(tenancy)

        client.get(f"/invite/{invitation.token}/")

        assert client.session[PENDING_INVITE_SESSION_KEY] == invitation.token

    def test_an_expired_token_falls_back_to_a_fresh_org(self, tenancy, client):
        from django.utils import timezone

        invitation = self._pending_invite(tenancy)
        client.get(f"/invite/{invitation.token}/")
        invitation.expires_at = timezone.now() - timezone.timedelta(seconds=1)
        invitation.save(update_fields=["expires_at"])

        client.post(
            reverse("account_signup"),
            {"email": "invitee@example.test", "password1": TEST_PASSWORD},
            follow=True,
        )

        membership = OrgMembership.objects.get(user__email="invitee@example.test")
        assert membership.organization_id != tenancy.organization.pk


@pytest.mark.django_db
class TestFirstVisitProvisioning:
    def test_a_superuser_created_from_the_shell_is_provisioned_on_first_visit(self, client_for):
        """createsuperuser writes a User row and fires no signup signal."""
        admin = create_user("root@example.test", is_staff=True, is_superuser=True)

        response = client_for(admin).get("/")

        assert response.status_code == 302
        membership = OrgMembership.objects.get(user=admin)
        assert response.headers["Location"] == f"/w/{membership.user.last_workspace_id}/"

    def test_ensure_provisioned_is_a_no_op_for_existing_members(self, tenancy):
        before = Organization.objects.count()

        ensure_provisioned(tenancy.owner)

        assert Organization.objects.count() == before


@pytest.mark.django_db
class TestRootRouting:
    def test_anonymous_users_go_to_login(self, client):
        assert client.get("/").headers["Location"] == "/accounts/login/"

    def test_an_archived_last_workspace_falls_back(self, tenancy, client_for):
        tenancy.workspace.is_archived = True
        tenancy.workspace.save(update_fields=["is_archived"])

        response = client_for(tenancy.owner).get("/")

        assert response.headers["Location"] == "/organization/workspaces/"
