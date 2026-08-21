"""Invitation lifecycle and the escalation rules."""

from datetime import timedelta

import pytest
from django.core import mail
from django.utils import timezone

from apps.members.models import Invitation, OrgMembership, WorkspaceMembership
from apps.members.roles import OrgRole, WorkspaceRole
from apps.members.services import (
    MembershipError,
    accept_invitation,
    create_invitation,
    remove_member,
    resend_invitation,
    revoke_invitation,
    update_member_org_role,
    update_workspace_assignments,
    workspace_authority_level,
)
from apps.workspaces.models import Workspace
from tests.support import create_user


def _assignments(workspace, role):
    return [{"workspace_id": str(workspace.pk), "role": role}]


@pytest.mark.django_db
class TestCreateInvitation:
    def test_an_owner_can_invite(self, tenancy):
        invitation = create_invitation(
            org=tenancy.organization,
            email="New.Person@Example.test",
            org_role=OrgRole.MEMBER,
            workspace_assignments=_assignments(tenancy.workspace, WorkspaceRole.EDITOR),
            invited_by=tenancy.owner,
        )

        assert invitation.email == "new.person@example.test"
        assert invitation.expires_at > timezone.now()
        assert len(mail.outbox) == 1

    def test_nobody_can_invite_an_owner(self, tenancy):
        with pytest.raises(MembershipError, match="organization owner"):
            create_invitation(
                org=tenancy.organization,
                email="a@example.test",
                org_role=OrgRole.OWNER,
                workspace_assignments=[],
                invited_by=tenancy.owner,
            )

    def test_an_admin_cannot_clone_their_own_tier(self, tenancy):
        """A compromised admin must not be able to mint more admins."""
        admin = create_user("org.admin@acme.test")
        OrgMembership.objects.create(user=admin, organization=tenancy.organization, org_role=OrgRole.ADMIN)

        with pytest.raises(MembershipError, match="Only organization owners"):
            create_invitation(
                org=tenancy.organization,
                email="b@example.test",
                org_role=OrgRole.ADMIN,
                workspace_assignments=[],
                invited_by=admin,
            )

    def test_an_owner_can_invite_an_admin(self, tenancy):
        invitation = create_invitation(
            org=tenancy.organization,
            email="c@example.test",
            org_role=OrgRole.ADMIN,
            workspace_assignments=[],
            invited_by=tenancy.owner,
        )

        assert invitation.org_role == OrgRole.ADMIN

    def test_a_workspace_role_above_your_own_is_refused(self, tenancy):
        """Deviation: workspace authority requires manage_members *there*."""
        editor = tenancy.user_for("editor")

        with pytest.raises(MembershipError, match="manage members in that workspace"):
            create_invitation(
                org=tenancy.organization,
                email="d@example.test",
                org_role=OrgRole.MEMBER,
                workspace_assignments=_assignments(tenancy.workspace, WorkspaceRole.VIEWER),
                invited_by=editor,
            )

    def test_a_workspace_admin_who_is_only_an_org_member_can_invite(self, tenancy):
        """This is why the org-tier rule stops at the admin tier."""
        workspace_admin = tenancy.user_for("admin")

        invitation = create_invitation(
            org=tenancy.organization,
            email="e@example.test",
            org_role=OrgRole.MEMBER,
            workspace_assignments=_assignments(tenancy.workspace, WorkspaceRole.AGENT),
            invited_by=workspace_admin,
        )

        assert invitation.workspace_assignments[0]["role"] == WorkspaceRole.AGENT

    def test_another_orgs_workspace_is_refused(self, tenancy, other_tenancy):
        with pytest.raises(MembershipError, match="does not belong to your organization"):
            create_invitation(
                org=tenancy.organization,
                email="f@example.test",
                org_role=OrgRole.MEMBER,
                workspace_assignments=_assignments(other_tenancy.workspace, WorkspaceRole.VIEWER),
                invited_by=tenancy.owner,
            )

    def test_an_existing_member_is_refused(self, tenancy):
        with pytest.raises(MembershipError, match="already a member"):
            create_invitation(
                org=tenancy.organization,
                email=tenancy.user_for("viewer").email,
                org_role=OrgRole.MEMBER,
                workspace_assignments=[],
                invited_by=tenancy.owner,
            )

    def test_a_duplicate_pending_invite_is_refused(self, tenancy):
        create_invitation(
            org=tenancy.organization,
            email="dup@example.test",
            org_role=OrgRole.MEMBER,
            workspace_assignments=[],
            invited_by=tenancy.owner,
        )

        with pytest.raises(MembershipError, match="already pending"):
            create_invitation(
                org=tenancy.organization,
                email="dup@example.test",
                org_role=OrgRole.MEMBER,
                workspace_assignments=[],
                invited_by=tenancy.owner,
            )

    def test_a_mail_outage_does_not_lose_the_invitation(self, tenancy, monkeypatch):
        def explode(*args, **kwargs):
            raise OSError("smtp is down")

        monkeypatch.setattr("django.core.mail.EmailMultiAlternatives.send", explode)

        invitation = create_invitation(
            org=tenancy.organization,
            email="g@example.test",
            org_role=OrgRole.MEMBER,
            workspace_assignments=[],
            invited_by=tenancy.owner,
        )

        assert Invitation.objects.filter(pk=invitation.pk).exists()


@pytest.mark.django_db
class TestAcceptInvitation:
    def _invite(self, tenancy, email="joiner@example.test", role=WorkspaceRole.AGENT):
        return create_invitation(
            org=tenancy.organization,
            email=email,
            org_role=OrgRole.MEMBER,
            workspace_assignments=_assignments(tenancy.workspace, role),
            invited_by=tenancy.owner,
        )

    def test_it_creates_both_memberships(self, tenancy):
        invitation = self._invite(tenancy)
        joiner = create_user("joiner@example.test")

        accept_invitation(invitation, joiner)

        assert OrgMembership.objects.filter(user=joiner, organization=tenancy.organization).exists()
        membership = WorkspaceMembership.objects.get(user=joiner, workspace=tenancy.workspace)
        assert membership.workspace_role == WorkspaceRole.AGENT
        joiner.refresh_from_db()
        assert joiner.last_workspace_id == tenancy.workspace.pk

    def test_the_org_membership_records_acceptance(self, tenancy):
        """Studio declares accepted_at on OrgMembership and never writes it."""
        invitation = self._invite(tenancy)
        joiner = create_user("joiner@example.test")

        accept_invitation(invitation, joiner)

        assert OrgMembership.objects.get(user=joiner).accepted_at is not None

    def test_a_different_email_is_refused_by_default(self, tenancy):
        invitation = self._invite(tenancy)
        stranger = create_user("someone.else@example.test")

        with pytest.raises(MembershipError, match="different email"):
            accept_invitation(invitation, stranger)

    def test_the_signup_path_may_skip_the_email_check(self, tenancy):
        """A social login returns whatever address the provider owns; the
        session-bound token is the proof of delivery."""
        invitation = self._invite(tenancy)
        stranger = create_user("google.address@example.test")

        accept_invitation(invitation, stranger, require_email_match=False)

        assert OrgMembership.objects.filter(user=stranger, organization=tenancy.organization).exists()

    def test_an_expired_invitation_is_refused(self, tenancy):
        invitation = self._invite(tenancy)
        invitation.expires_at = timezone.now() - timedelta(seconds=1)
        invitation.save(update_fields=["expires_at"])
        joiner = create_user("joiner@example.test")

        with pytest.raises(MembershipError, match="expired"):
            accept_invitation(invitation, joiner)

    def test_it_cannot_be_replayed(self, tenancy):
        invitation = self._invite(tenancy)
        joiner = create_user("joiner@example.test")
        accept_invitation(invitation, joiner)

        with pytest.raises(MembershipError, match="already been accepted"):
            accept_invitation(invitation, joiner)


@pytest.mark.django_db
class TestResendAndRevoke:
    def _invite(self, tenancy):
        return create_invitation(
            org=tenancy.organization,
            email="h@example.test",
            org_role=OrgRole.MEMBER,
            workspace_assignments=[],
            invited_by=tenancy.owner,
        )

    def test_resend_rotates_the_token(self, tenancy):
        """Rotation is what makes resend the repair for a leaked or stale link."""
        invitation = self._invite(tenancy)
        original_digest = invitation.token_digest
        original_token = invitation.raw_token

        resend_invitation(invitation)

        assert invitation.token_digest != original_digest
        assert invitation.raw_token != original_token
        assert not Invitation.objects.for_token(original_token).exists()

    def test_revoke_expires_it(self, tenancy):
        invitation = self._invite(tenancy)

        revoke_invitation(invitation)

        assert invitation.is_expired
        assert not invitation.is_pending


@pytest.mark.django_db
class TestRemoveMember:
    def test_you_cannot_remove_yourself(self, tenancy):
        membership = tenancy.org_membership

        with pytest.raises(MembershipError, match="cannot remove yourself"):
            remove_member(tenancy.organization, membership, tenancy.owner)

    def test_you_cannot_remove_the_last_owner(self, tenancy):
        second_owner = create_user("second.owner@acme.test")
        membership = OrgMembership.objects.create(
            user=second_owner, organization=tenancy.organization, org_role=OrgRole.OWNER
        )
        remove_member(tenancy.organization, tenancy.org_membership, second_owner)

        with pytest.raises(MembershipError, match="last organization owner"):
            remove_member(tenancy.organization, membership, tenancy.user_for("admin"))

    def test_removing_a_member_clears_their_workspace_memberships(self, tenancy):
        membership = OrgMembership.objects.get(user=tenancy.user_for("agent"))

        remove_member(tenancy.organization, membership, tenancy.owner)

        assert not WorkspaceMembership.objects.filter(user=tenancy.user_for("agent")).exists()

    def test_you_cannot_remove_someone_senior_to_you(self, tenancy):
        admin = create_user("org.admin@acme.test")
        OrgMembership.objects.create(user=admin, organization=tenancy.organization, org_role=OrgRole.ADMIN)
        # A second owner, so the failure is about authority rather than the
        # last-owner rule.
        spare_owner = create_user("spare.owner@acme.test")
        OrgMembership.objects.create(user=spare_owner, organization=tenancy.organization, org_role=OrgRole.OWNER)

        with pytest.raises(MembershipError, match="higher than your own"):
            remove_member(tenancy.organization, tenancy.org_membership, admin)


@pytest.mark.django_db
class TestUpdateOrgRole:
    def test_promotion_to_owner_is_refused(self, tenancy):
        membership = OrgMembership.objects.get(user=tenancy.user_for("editor"))

        with pytest.raises(MembershipError, match="Transfer ownership"):
            update_member_org_role(tenancy.organization, membership, OrgRole.OWNER, caller=tenancy.owner)

    def test_an_admin_cannot_promote_to_admin(self, tenancy):
        admin = create_user("org.admin@acme.test")
        OrgMembership.objects.create(user=admin, organization=tenancy.organization, org_role=OrgRole.ADMIN)
        membership = OrgMembership.objects.get(user=tenancy.user_for("editor"))

        with pytest.raises(MembershipError, match="Only organization owners"):
            update_member_org_role(tenancy.organization, membership, OrgRole.ADMIN, caller=admin)

    def test_an_admin_cannot_demote_an_owner(self, tenancy):
        admin = create_user("org.admin@acme.test")
        OrgMembership.objects.create(user=admin, organization=tenancy.organization, org_role=OrgRole.ADMIN)

        with pytest.raises(MembershipError, match="higher than your own"):
            update_member_org_role(tenancy.organization, tenancy.org_membership, OrgRole.MEMBER, caller=admin)

    def test_the_last_owner_cannot_be_demoted(self, tenancy):
        with pytest.raises(MembershipError, match="last organization owner"):
            update_member_org_role(tenancy.organization, tenancy.org_membership, OrgRole.MEMBER, caller=tenancy.owner)


@pytest.mark.django_db
class TestWorkspaceAssignments:
    def test_an_owner_may_grant_anything_in_their_org(self, tenancy):
        assert workspace_authority_level(tenancy.owner, tenancy.organization, tenancy.workspace.pk) == 4

    @pytest.mark.parametrize("role", ["editor", "agent", "viewer"])
    def test_roles_without_manage_members_have_no_authority(self, tenancy, role):
        user = tenancy.user_for(role)

        assert workspace_authority_level(user, tenancy.organization, tenancy.workspace.pk) == 0

    def test_a_workspace_admin_has_authority_there(self, tenancy):
        user = tenancy.user_for("admin")

        assert workspace_authority_level(user, tenancy.organization, tenancy.workspace.pk) == 4

    def test_a_workspace_admin_has_no_authority_elsewhere(self, tenancy):
        other = Workspace.objects.create(organization=tenancy.organization, name="other")
        user = tenancy.user_for("admin")

        assert workspace_authority_level(user, tenancy.organization, other.pk) == 0

    def test_omission_removes_the_membership(self, tenancy):
        target = tenancy.user_for("agent")

        update_workspace_assignments(tenancy.organization, target, [], inviter=tenancy.owner)

        assert not WorkspaceMembership.objects.filter(user=target, workspace=tenancy.workspace).exists()

    def test_the_last_workspace_admin_cannot_be_removed(self, tenancy):
        """Otherwise the workspace has nobody who can manage its channels,
        settings or people."""
        WorkspaceMembership.objects.filter(user=tenancy.owner, workspace=tenancy.workspace).delete()
        target = tenancy.user_for("admin")

        with pytest.raises(MembershipError, match="last admin of a workspace"):
            update_workspace_assignments(tenancy.organization, target, [], inviter=tenancy.owner)

    def test_a_role_above_your_authority_is_refused(self, tenancy):
        editor = tenancy.user_for("editor")
        target = tenancy.user_for("viewer")

        with pytest.raises(MembershipError):
            update_workspace_assignments(
                tenancy.organization,
                target,
                _assignments(tenancy.workspace, WorkspaceRole.ADMIN),
                inviter=editor,
            )

    def test_you_cannot_demote_someone_you_have_no_authority_over(self, tenancy):
        """Studio's second-phase check: without it, submitting the form with a
        lower role passes the 'not higher than mine' test and the demotion
        itself is the privilege violation."""
        second_workspace = Workspace.objects.create(organization=tenancy.organization, name="second")
        WorkspaceMembership.objects.create(
            user=tenancy.user_for("admin"), workspace=second_workspace, workspace_role=WorkspaceRole.ADMIN
        )
        outsider = tenancy.user_for("editor")

        with pytest.raises(MembershipError):
            update_workspace_assignments(
                tenancy.organization,
                tenancy.user_for("admin"),
                _assignments(second_workspace, WorkspaceRole.VIEWER),
                inviter=outsider,
            )
