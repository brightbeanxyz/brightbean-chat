"""The service-layer writes the platform API reuses: workspaces, direct members, system-issued keys."""

import threading

import pytest
from django.db import connection, transaction

from apps.accounts.models import User
from apps.accounts.services import ExternalIdInUseError, provision_organization_and_workspace
from apps.api.services import ApiKeysError, issue_api_key, issue_api_key_as_system
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.members.roles import OrgRole, WorkspaceRole
from apps.members.services import MembershipError, accept_invitation, add_member_directly, create_invitation
from apps.organizations.models import Organization
from apps.organizations.services import WorkspaceError, create_workspace, set_workspace_archived
from apps.workspaces.models import Workspace
from tests.support import create_user


@pytest.mark.django_db
class TestCreateWorkspace:
    def test_a_workspace_without_a_creator_has_no_members(self, tenancy):
        workspace = create_workspace(tenancy.organization, "  Support  ")

        assert workspace.name == "Support"
        assert not WorkspaceMembership.objects.filter(workspace=workspace).exists()

    def test_single_workspace_mode_refuses_creation_but_not_provisioning(self, tenancy, settings):
        settings.SINGLE_WORKSPACE_MODE = True

        with pytest.raises(WorkspaceError) as exc:
            create_workspace(tenancy.organization, "Second")
        assert exc.value.code == "single_workspace"

        newcomer = create_user("fresh@example.test")
        org = provision_organization_and_workspace(newcomer)
        assert Workspace.objects.filter(organization=org).count() == 1

    def test_single_workspace_mode_guards_archiving_inside_the_service(self, tenancy, settings):
        settings.SINGLE_WORKSPACE_MODE = True
        spare = Workspace.objects.create(organization=tenancy.organization, name="Spare", is_archived=True)

        for target, archived in ((tenancy.workspace, True), (spare, False)):
            with pytest.raises(WorkspaceError) as exc:
                set_workspace_archived(tenancy.organization, target, archived=archived)
            assert exc.value.code == "single_workspace"

        Workspace.objects.filter(pk=tenancy.workspace.pk).update(is_archived=True)
        set_workspace_archived(tenancy.organization, spare, archived=False)
        spare.refresh_from_db()
        assert spare.is_archived is False

    def test_the_creator_becomes_its_admin(self, tenancy):
        workspace = create_workspace(tenancy.organization, "Sales", creator=tenancy.owner)

        membership = WorkspaceMembership.objects.get(workspace=workspace, user=tenancy.owner)
        assert membership.workspace_role == WorkspaceRole.ADMIN

    def test_a_duplicate_name_is_refused(self, tenancy):
        with pytest.raises(WorkspaceError) as exc:
            create_workspace(tenancy.organization, tenancy.workspace.name)

        assert exc.value.code == "duplicate"

    def test_a_blank_name_is_refused(self, tenancy):
        with pytest.raises(WorkspaceError):
            create_workspace(tenancy.organization, "   ")

    def test_the_session_view_still_creates_through_it(self, client_for, tenancy):
        response = client_for(tenancy.owner).post("/organization/workspaces/create/", {"name": "Via view"})

        assert response.status_code == 302
        assert Workspace.objects.filter(organization=tenancy.organization, name="Via view").exists()


@pytest.mark.django_db
class TestAddMemberDirectly:
    def test_it_writes_both_memberships_with_no_invitation(self, tenancy):
        user = create_user("direct@example.test")

        _, created = add_member_directly(
            tenancy.organization,
            user,
            OrgRole.MEMBER,
            [{"workspace_id": str(tenancy.workspace.pk), "role": WorkspaceRole.EDITOR}],
        )

        assert created is True

        assert OrgMembership.objects.get(user=user).org_role == OrgRole.MEMBER
        assert WorkspaceMembership.objects.get(user=user).workspace_role == WorkspaceRole.EDITOR
        user.refresh_from_db()
        assert user.last_workspace_id == tenancy.workspace.pk

    def test_it_is_idempotent_and_leaves_an_existing_member_alone(self, tenancy):
        user = tenancy.user_for(WorkspaceRole.EDITOR)

        membership, created = add_member_directly(tenancy.organization, user, OrgRole.ADMIN, [])

        assert created is False
        assert membership.org_role == OrgRole.MEMBER

    def test_nobody_is_added_as_an_owner(self, tenancy):
        with pytest.raises(MembershipError, match="owner"):
            add_member_directly(tenancy.organization, create_user("o@example.test"), OrgRole.OWNER, [])

    def test_a_workspace_of_another_organization_is_refused(self, tenancy, other_tenancy):
        with pytest.raises(MembershipError):
            add_member_directly(
                tenancy.organization,
                create_user("x@example.test"),
                OrgRole.MEMBER,
                [{"workspace_id": str(other_tenancy.workspace.pk), "role": WorkspaceRole.VIEWER}],
            )

    def test_an_account_in_another_organization_is_refused(self, tenancy, other_tenancy):
        with pytest.raises(MembershipError, match="already belongs"):
            add_member_directly(tenancy.organization, other_tenancy.owner, OrgRole.MEMBER, [])

    def test_a_refusal_leaves_no_membership_behind(self, tenancy, other_tenancy):
        user = create_user("half@example.test")

        with pytest.raises(MembershipError):
            add_member_directly(
                tenancy.organization,
                user,
                OrgRole.MEMBER,
                [{"workspace_id": str(other_tenancy.workspace.pk), "role": WorkspaceRole.VIEWER}],
            )

        assert not OrgMembership.objects.filter(user=user).exists()

    def test_an_archived_workspace_is_a_refusal_not_a_dropped_assignment(self, tenancy):
        Workspace.objects.filter(pk=tenancy.workspace.pk).update(is_archived=True)

        with pytest.raises(MembershipError):
            add_member_directly(
                tenancy.organization,
                create_user("arch@example.test"),
                OrgRole.MEMBER,
                [{"workspace_id": str(tenancy.workspace.pk), "role": WorkspaceRole.VIEWER}],
            )


@pytest.mark.django_db
class TestAcceptingStillRefusesASecondOrganization:
    """The one-organization-per-account rule moved into a helper shared with
    ``add_member_directly``; this pins that accepting an invitation still hits it."""

    def test_accepting_into_a_second_organization_is_refused(self, tenancy, other_tenancy):
        invitation = create_invitation(
            org=tenancy.organization,
            email=other_tenancy.owner.email,
            org_role=OrgRole.MEMBER,
            workspace_assignments=[{"workspace_id": str(tenancy.workspace.pk), "role": WorkspaceRole.VIEWER}],
            invited_by=tenancy.owner,
        )

        with pytest.raises(MembershipError, match="already belongs"):
            accept_invitation(invitation, other_tenancy.owner)

        assert OrgMembership.objects.filter(user=other_tenancy.owner).count() == 1


@pytest.mark.django_db
class TestProvisionOnBehalf:
    def test_a_taken_external_id_is_a_domain_error_and_creates_nothing(self):
        provision_organization_and_workspace(create_user("a@example.test"), external_id="t-1")
        second = create_user("b@example.test")

        with pytest.raises(ExternalIdInUseError):
            provision_organization_and_workspace(second, external_id="t-1")

        assert not OrgMembership.objects.filter(user=second).exists()
        assert Organization.objects.count() == 1

    def test_a_blank_external_id_is_stored_as_null(self):
        first = provision_organization_and_workspace(create_user("a@example.test"), external_id="")
        second = provision_organization_and_workspace(create_user("b@example.test"), external_id="")

        assert first.external_id is None and second.external_id is None

    def test_saving_a_blank_external_id_stores_null(self):
        a = Organization.objects.create(name="A", external_id="")
        b = Organization.objects.create(name="B", external_id="  ")

        assert a.external_id is None and b.external_id is None

    def test_names_and_external_id_are_used(self):
        user = create_user("owner@example.test")

        org = provision_organization_and_workspace(user, org_name="Acme", workspace_name="Main", external_id="t-1")

        assert (org.name, org.external_id) == ("Acme", "t-1")
        assert Workspace.objects.get(organization=org).name == "Main"
        assert Organization.objects.filter(external_id="t-1").count() == 1


@pytest.mark.django_db
class TestSystemIssuedKey:
    def test_no_issuer_is_needed_and_the_key_carries_no_creator(self, tenancy):
        key = issue_api_key_as_system(workspace=tenancy.workspace, name="zirek", scopes=["read", "write", "erase"])

        assert key.raw_token.startswith("bb_")
        assert key.created_by is None
        assert key.scopes == ["erase", "read", "write"]

    def test_scope_and_name_validation_still_apply(self, tenancy):
        with pytest.raises(ApiKeysError):
            issue_api_key_as_system(workspace=tenancy.workspace, name="x", scopes=["root"])
        with pytest.raises(ApiKeysError):
            issue_api_key_as_system(workspace=tenancy.workspace, name=" ", scopes=["read"])

    def test_the_member_path_fails_closed_on_a_missing_issuer(self, tenancy):
        """A caller that lost track of the user must not mint an unrestricted key."""
        with pytest.raises(ApiKeysError):
            issue_api_key(workspace=tenancy.workspace, issuer=None, name="x", scopes=["read"])


@pytest.mark.django_db(transaction=True)
class TestOneOrganizationPerAccountIsSerialised:
    """The rule is a read followed by an insert, and the uniqueness constraint is
    on (user, organization) — so it only holds if both happen under the user's
    row lock. Deterministic rather than a race: hold the lock, start the writer,
    and check it waits.

    The held lock is ``FOR NO KEY UPDATE`` on purpose. A plain ``FOR UPDATE``
    also blocks the membership INSERT's own ``FOR KEY SHARE`` on the user, so the
    writer would wait with or without the fix and the test would prove nothing.
    This one conflicts only with the explicit ``FOR UPDATE`` the code takes."""

    def test_attaching_a_member_waits_for_that_users_row(self, tenancy):
        user = create_user("raced@example.test")
        outcome: dict[str, object] = {}

        def writer() -> None:
            try:
                add_member_directly(tenancy.organization, user, OrgRole.MEMBER, [])
                outcome["done"] = True
            except Exception as exc:  # pragma: no cover - reported by the assertion below
                outcome["error"] = exc
            finally:
                connection.close()

        with transaction.atomic():
            User.objects.select_for_update(no_key=True).get(pk=user.pk)
            thread = threading.Thread(target=writer)
            thread.start()
            thread.join(timeout=1.0)
            assert thread.is_alive(), "the writer did not wait for the user's row lock"
        thread.join(timeout=10)

        assert outcome == {"done": True}
        assert OrgMembership.objects.filter(user=user).count() == 1

    def test_a_concurrent_duplicate_reports_the_existing_membership(self, tenancy):
        user = create_user("twice@example.test")
        outcome: dict[str, object] = {}

        def writer() -> None:
            try:
                outcome["result"] = add_member_directly(tenancy.organization, user, OrgRole.MEMBER, [])
            except Exception as exc:  # pragma: no cover - reported by the assertion below
                outcome["error"] = exc
            finally:
                connection.close()

        with transaction.atomic():
            User.objects.select_for_update(no_key=True).get(pk=user.pk)
            thread = threading.Thread(target=writer)
            thread.start()
            thread.join(timeout=1.0)
            assert thread.is_alive()
            OrgMembership.objects.create(user=user, organization=tenancy.organization, org_role=OrgRole.MEMBER)
        thread.join(timeout=10)

        assert "error" not in outcome
        assert outcome["result"][1] is False  # type: ignore[index]


@pytest.mark.django_db
class TestReviewFollowUps:
    def test_a_whitespace_only_workspace_name_gets_the_default_description(self, db):
        from apps.organizations.services import DEFAULT_WORKSPACE_DESCRIPTION, create_organization

        org, _ = create_organization(name="Acme", owner_email="o@example.test", workspace_name="   ")

        workspace = Workspace.objects.get(organization=org)
        assert workspace.description == DEFAULT_WORKSPACE_DESCRIPTION

    def test_revoking_through_a_stale_instance_keeps_the_first_timestamp(self, tenancy):
        from apps.api.models import ApiKey
        from apps.api.services import revoke_api_key
        from apps.api.tests.conftest import make_key

        key, _ = make_key(tenancy.workspace)
        scoped = ApiKey.objects.for_workspace(tenancy.workspace)
        stale = scoped.get(pk=key.pk)

        assert revoke_api_key(key) is True
        first = scoped.get(pk=key.pk).revoked_at
        assert revoke_api_key(stale) is False
        assert scoped.get(pk=key.pk).revoked_at == first
