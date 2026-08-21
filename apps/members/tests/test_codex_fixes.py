"""Regressions for the Codex review findings."""

import pytest

from apps.common.encryption import hmac_digest
from apps.members.models import Invitation, OrgMembership, WorkspaceMembership
from apps.members.roles import OrgRole, WorkspaceRole
from apps.members.services import (
    MembershipError,
    accept_invitation,
    create_invitation,
    remove_member,
)
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace
from tests.support import TEST_PASSWORD, create_user


def _invite(tenancy, email="joiner@example.test", role=WorkspaceRole.AGENT):
    return create_invitation(
        org=tenancy.organization,
        email=email,
        org_role=OrgRole.MEMBER,
        workspace_assignments=[{"workspace_id": str(tenancy.workspace.pk), "role": role.value}],
        invited_by=tenancy.owner,
    )


@pytest.mark.django_db
class TestTokensAreNotStored:
    """The token is the whole credential; a column holding it means a database
    snapshot hands over every pending invitation (SECURITY-BASELINE §5)."""

    def test_the_row_has_no_token_column(self):
        assert "token" not in {field.name for field in Invitation._meta.get_fields()}

    def test_the_stored_value_is_a_keyed_digest(self, tenancy):
        invitation = _invite(tenancy)

        assert invitation.token_digest == hmac_digest(invitation.raw_token)
        assert invitation.raw_token not in invitation.token_digest

    def test_the_raw_token_never_comes_back_from_the_database(self, tenancy):
        invitation = _invite(tenancy)

        reloaded = Invitation.objects.get(pk=invitation.pk)

        assert reloaded.raw_token is None

    def test_lookup_by_token_still_works(self, tenancy):
        invitation = _invite(tenancy)

        assert Invitation.objects.for_token(invitation.raw_token).get() == invitation

    def test_a_wrong_token_matches_nothing(self, tenancy):
        _invite(tenancy)

        assert not Invitation.objects.for_token("not-the-token").exists()

    def test_an_empty_token_matches_nothing(self, tenancy):
        _invite(tenancy)

        assert not Invitation.objects.for_token("").exists()

    def test_the_digest_is_keyed_on_the_secret_key(self, settings):
        before = hmac_digest("same-input")
        settings.SECRET_KEY = "a-different-secret-key"

        assert hmac_digest("same-input") != before


@pytest.mark.django_db
class TestTokensStayOutOfLogs:
    """The token reaches logs through the request line, not through any
    key=value pair: runserver prints every path at INFO and django.request logs
    the path on a 500."""

    def test_an_invite_path_is_redacted(self):
        from apps.common.logging import REDACTED, scrub

        line = 'GET /invite/ah7rONWhDblFRFPlRt8_m0CWQbCO5Z48j5lI2GnoiuU/ HTTP/1.1" 200'

        scrubbed = scrub(line)

        assert "ah7rONWhDblFRFPlRt8" not in scrubbed
        assert REDACTED in scrubbed

    def test_an_absolute_invite_url_is_redacted(self):
        from apps.common.logging import scrub

        assert "SECRET" not in scrub("https://chat.example.com/invite/SECRETSECRETSECRET/")

    def test_unrelated_paths_are_left_alone(self):
        from apps.common.logging import scrub

        assert scrub("GET /organization/members/ HTTP/1.1") == "GET /organization/members/ HTTP/1.1"


@pytest.mark.django_db
class TestInvitationIsSingleUseUnderConcurrency:
    def test_acceptance_re_reads_the_row_under_a_lock(self):
        """The caller looked the invitation up before the transaction opened, so
        validating that stale instance would let two acceptances of one token
        both pass the accepted_at check."""
        import inspect

        from apps.members import services

        source = inspect.getsource(services.accept_invitation)
        lock_at = source.index("select_for_update")
        check_at = source.index("if invitation.is_accepted")

        assert lock_at < check_at

    def test_a_stale_instance_cannot_replay(self, tenancy):
        invitation = _invite(tenancy)
        stale = Invitation.objects.get(pk=invitation.pk)
        accept_invitation(invitation, create_user("joiner@example.test"))

        with pytest.raises(MembershipError, match="already been accepted"):
            accept_invitation(stale, create_user("second@example.test"), require_email_match=False)


@pytest.mark.django_db
class TestOneOrganizationPerAccount:
    """v1 routes org-scoped pages from a single OrgMembership, so a second one
    leaves request.org and last_workspace_id pointing at different orgs."""

    def test_accepting_into_a_second_org_is_refused(self, tenancy, other_tenancy):
        invitation = _invite(tenancy, email=other_tenancy.owner.email)

        with pytest.raises(MembershipError, match="already belongs to"):
            accept_invitation(invitation, other_tenancy.owner)

    def test_no_membership_is_left_behind(self, tenancy, other_tenancy):
        invitation = _invite(tenancy, email=other_tenancy.owner.email)

        with pytest.raises(MembershipError):
            accept_invitation(invitation, other_tenancy.owner)

        assert OrgMembership.objects.filter(user=other_tenancy.owner).count() == 1

    def test_the_message_says_which_organization(self, tenancy, other_tenancy):
        invitation = _invite(tenancy, email=other_tenancy.owner.email)

        with pytest.raises(MembershipError, match=other_tenancy.organization.name):
            accept_invitation(invitation, other_tenancy.owner)

    def test_a_fresh_account_still_joins(self, tenancy):
        invitation = _invite(tenancy)

        accept_invitation(invitation, create_user("joiner@example.test"))

        assert OrgMembership.objects.filter(user__email="joiner@example.test").count() == 1

    def test_the_check_targets_a_different_org_not_any_membership(self):
        """A membership in the *inviting* org must not be mistaken for a
        conflict — accepting would then be impossible for the invitee the
        signup flow already provisioned into it."""
        import inspect

        from apps.members import services

        source = inspect.getsource(services.accept_invitation)

        assert ".exclude(organization=invitation.organization)" in source


@pytest.mark.django_db
class TestInviteLinkSurvivesLogin:
    """An existing account clicking an invite link used to land on / after
    signing in, and user_signed_up never fires for a login — so the invitation
    was silently never accepted."""

    def test_the_login_link_carries_next(self, tenancy, client):
        invitation = _invite(tenancy)

        content = client.get(f"/invite/{invitation.raw_token}/").content.decode()

        assert f"/accounts/login/?next=%2Finvite%2F{invitation.raw_token}%2F" in content

    def test_the_signup_link_carries_next(self, tenancy, client):
        invitation = _invite(tenancy)

        content = client.get(f"/invite/{invitation.raw_token}/").content.decode()

        assert "/accounts/signup/?next=%2Finvite%2F" in content

    def test_an_existing_account_lands_back_on_the_invitation(self, tenancy, client):
        invitation = _invite(tenancy, email="already@example.test")
        create_user("already@example.test")
        invite_url = f"/invite/{invitation.raw_token}/"
        client.get(invite_url)

        response = client.post(
            f"/accounts/login/?next={invite_url}",
            {"login": "already@example.test", "password": TEST_PASSWORD},
            follow=True,
        )

        assert response.redirect_chain[-1][0] == invite_url
        assert b"Join" in response.content

    def test_and_can_then_accept(self, tenancy, client_for):
        invitation = _invite(tenancy, email="already@example.test")
        joiner = create_user("already@example.test")

        client_for(joiner).post(f"/invite/{invitation.raw_token}/", follow=True)

        assert OrgMembership.objects.filter(user=joiner, organization=tenancy.organization).exists()


@pytest.mark.django_db
class TestWorkspaceAdminCanInvite:
    """A workspace Admin whose org role is Member is a supported inviter — that
    combination is the whole reason manage_members is a workspace permission."""

    def test_they_reach_the_endpoint(self, tenancy, client_for):
        client = client_for(tenancy.user_for("admin"))

        client.post(
            "/organization/members/invite/",
            {
                "email": "collaborator@example.test",
                "org_role": OrgRole.MEMBER.value,
                f"ws_{tenancy.workspace.pk}": "1",
                f"ws_role_{tenancy.workspace.pk}": "agent",
            },
            follow=True,
        )

        assert Invitation.objects.for_org(tenancy.organization.pk).filter(email="collaborator@example.test").exists()

    def test_the_form_is_offered_to_them(self, tenancy, client_for):
        content = client_for(tenancy.user_for("admin")).get("/organization/members/").content

        assert b"Invite someone" in content

    def test_they_cannot_invite_with_no_workspace(self, tenancy):
        with pytest.raises(MembershipError, match="at least one workspace"):
            create_invitation(
                org=tenancy.organization,
                email="nowhere@example.test",
                org_role=OrgRole.MEMBER,
                workspace_assignments=[],
                invited_by=tenancy.user_for("admin"),
            )

    def test_an_org_admin_may_still_invite_with_no_workspace(self, tenancy):
        admin = create_user("org.admin@acme.test")
        OrgMembership.objects.create(user=admin, organization=tenancy.organization, org_role=OrgRole.ADMIN)

        invitation = create_invitation(
            org=tenancy.organization,
            email="orgonly@example.test",
            org_role=OrgRole.MEMBER,
            workspace_assignments=[],
            invited_by=admin,
        )

        assert invitation.workspace_assignments == []


@pytest.mark.django_db
class TestRemovingAMemberCannotOrphanAWorkspace:
    """The bulk delete bypassed the invariant update_workspace_assignments
    enforces, leaving a workspace with nobody who can manage it."""

    def test_the_last_workspace_admin_cannot_be_removed(self, tenancy):
        WorkspaceMembership.objects.filter(user=tenancy.owner, workspace=tenancy.workspace).delete()
        membership = OrgMembership.objects.get(user=tenancy.user_for("admin"))

        with pytest.raises(MembershipError, match="last admin of a workspace"):
            remove_member(tenancy.organization, membership, tenancy.owner)

    def test_nothing_is_deleted_when_it_is_refused(self, tenancy):
        WorkspaceMembership.objects.filter(user=tenancy.owner, workspace=tenancy.workspace).delete()
        membership = OrgMembership.objects.get(user=tenancy.user_for("admin"))

        with pytest.raises(MembershipError):
            remove_member(tenancy.organization, membership, tenancy.owner)

        assert OrgMembership.objects.filter(pk=membership.pk).exists()
        assert WorkspaceMembership.objects.filter(user=tenancy.user_for("admin")).exists()

    def test_a_member_who_is_not_the_last_admin_is_removable(self, tenancy):
        membership = OrgMembership.objects.get(user=tenancy.user_for("admin"))

        remove_member(tenancy.organization, membership, tenancy.owner)

        assert not OrgMembership.objects.filter(pk=membership.pk).exists()

    def test_an_archived_workspace_still_counts(self, tenancy):
        """Archived is a filter on navigation, not a licence to orphan it."""
        archived = Workspace.objects.create(organization=tenancy.organization, name="Archived")
        WorkspaceMembership.objects.create(
            user=tenancy.user_for("editor"), workspace=archived, workspace_role=WorkspaceRole.ADMIN
        )
        archived.is_archived = True
        archived.save(update_fields=["is_archived"])
        membership = OrgMembership.objects.get(user=tenancy.user_for("editor"))

        with pytest.raises(MembershipError, match="last admin of a workspace"):
            remove_member(tenancy.organization, membership, tenancy.owner)


@pytest.mark.django_db
class TestRenamingOntoAnExistingName:
    def test_it_is_a_message_not_a_500(self, tenancy, client_for):
        Workspace.objects.create(organization=tenancy.organization, name="Taken")

        response = client_for(tenancy.user_for("admin")).post(
            f"/w/{tenancy.workspace.pk}/settings/update/", {"name": "Taken"}, follow=True
        )

        assert response.status_code == 200
        assert b"already has that name" in response.content

    def test_renaming_to_its_own_name_is_fine(self, tenancy, client_for):
        client_for(tenancy.user_for("admin")).post(
            f"/w/{tenancy.workspace.pk}/settings/update/",
            {"name": tenancy.workspace.name, "description": "changed"},
        )

        tenancy.workspace.refresh_from_db()
        assert tenancy.workspace.description == "changed"

    def test_another_organizations_name_is_not_a_clash(self, tenancy, other_tenancy, client_for):
        client_for(tenancy.user_for("admin")).post(
            f"/w/{tenancy.workspace.pk}/settings/update/", {"name": other_tenancy.workspace.name}
        )

        tenancy.workspace.refresh_from_db()
        assert tenancy.workspace.name == other_tenancy.workspace.name


@pytest.mark.django_db
class TestOrganizationsStayIsolated:
    def test_an_invitation_cannot_name_another_orgs_workspace(self, tenancy, other_tenancy):
        with pytest.raises(MembershipError, match="does not belong to your organization"):
            create_invitation(
                org=tenancy.organization,
                email="x@example.test",
                org_role=OrgRole.MEMBER,
                workspace_assignments=[{"workspace_id": str(other_tenancy.workspace.pk), "role": "viewer"}],
                invited_by=tenancy.owner,
            )

    def test_a_token_from_one_org_does_not_resolve_in_another(self, tenancy, other_tenancy):
        invitation = _invite(tenancy)

        digest = hmac_digest(invitation.raw_token)

        assert not Invitation.objects.for_org(other_tenancy.organization.pk).filter(token_digest=digest).exists()


@pytest.mark.django_db
def test_organizations_are_unaffected_by_the_digest_change(tenancy):
    assert Organization.objects.filter(pk=tenancy.organization.pk).exists()
