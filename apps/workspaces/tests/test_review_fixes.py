"""Regressions for the review findings in apps/workspaces and apps/organizations."""

import pytest

from apps.members.models import OrgMembership, WorkspaceMembership
from apps.members.roles import OrgRole
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace
from tests.support import create_user


def count_queries(client, url: str) -> int:
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    client.get(url)
    with CaptureQueriesContext(connection) as captured:
        client.get(url)
    return len(captured)


@pytest.mark.django_db
class TestNamesAreNotBlankable:
    """`or workspace.name` only catches an empty string, so "   " survived it
    and was stored as a nameless workspace."""

    def test_a_whitespace_workspace_name_is_refused(self, tenancy, client_for):
        response = client_for(tenancy.user_for("admin")).post(
            f"/w/{tenancy.workspace.pk}/settings/update/", {"name": "   "}, follow=True
        )

        tenancy.workspace.refresh_from_db()
        assert tenancy.workspace.name != ""
        assert b"needs a name" in response.content

    def test_a_whitespace_organization_name_is_refused(self, tenancy, client_for):
        original = tenancy.organization.name

        response = client_for(tenancy.owner).post("/organization/settings/update/", {"name": "  "}, follow=True)

        tenancy.organization.refresh_from_db()
        assert tenancy.organization.name == original
        assert b"needs a name" in response.content

    def test_a_real_name_still_saves_trimmed(self, tenancy, client_for):
        client_for(tenancy.user_for("admin")).post(
            f"/w/{tenancy.workspace.pk}/settings/update/", {"name": "  Renamed  "}
        )

        tenancy.workspace.refresh_from_db()
        assert tenancy.workspace.name == "Renamed"


@pytest.mark.django_db
class TestDashboardDoesNotScaleWithMemberships:
    def test_the_switcher_is_a_fixed_number_of_queries(self, tenancy, client_for):
        client = client_for(tenancy.owner)
        url = f"/w/{tenancy.workspace.pk}/"
        before = count_queries(client, url)

        for index in range(8):
            extra = Workspace.objects.create(organization=tenancy.organization, name=f"extra-{index}")
            WorkspaceMembership.objects.create(user=tenancy.owner, workspace=extra, workspace_role="admin")

        assert count_queries(client, url) == before

    def test_archived_workspaces_are_filtered_in_the_database(self, tenancy, client_for):
        archived = Workspace.objects.create(organization=tenancy.organization, name="Archived one")
        WorkspaceMembership.objects.create(user=tenancy.owner, workspace=archived, workspace_role="admin")
        archived.is_archived = True
        archived.save(update_fields=["is_archived"])

        content = client_for(tenancy.owner).get(f"/w/{tenancy.workspace.pk}/").content

        assert b"Archived one" not in content


@pytest.mark.django_db
class TestOrgWorkspaceListIsScoped:
    """The membership lookup had no organization filter — harmless while v1 is
    one org per user, and exactly the unscoped pattern CONTRIBUTING.md tells
    every later author not to write."""

    def test_a_membership_in_another_org_is_not_counted(self, tenancy, client_for):
        other_org = Organization.objects.create(name="Elsewhere")
        other_workspace = Workspace.objects.create(organization=other_org, name="Elsewhere workspace")
        OrgMembership.objects.create(user=tenancy.owner, organization=other_org, org_role=OrgRole.MEMBER)
        WorkspaceMembership.objects.create(user=tenancy.owner, workspace=other_workspace, workspace_role="admin")

        response = client_for(tenancy.owner).get("/organization/workspaces/")
        ids = response.context["member_workspace_ids"]

        assert other_workspace.pk not in ids
        assert tenancy.workspace.pk in ids


@pytest.mark.django_db
class TestCredentialPageQueryCount:
    def test_it_does_not_re_resolve_per_platform(self, tenancy, client_for, settings):
        """resolve_platform_credentials per platform re-read — and re-decrypted —
        rows the page had already fetched."""
        from apps.credentials.models import CONFIGURABLE_PLATFORMS

        client = client_for(tenancy.user_for("admin"))
        url = f"/w/{tenancy.workspace.pk}/settings/credentials/"

        queries = count_queries(client, url)

        # The property under test is the *per-platform* one: the budget is a
        # fixed base plus one query per platform, and a regression here would
        # scale with the platform count. The base moved 10 -> 11 when issue #7
        # added the unread-notification count to the shell's context processor,
        # which is one indexed count() on every authenticated page and does not
        # vary with platforms.
        assert queries < 11 + len(CONFIGURABLE_PLATFORMS), queries


@pytest.mark.django_db
def test_a_user_without_an_org_cannot_reach_the_workspace_list(client_for):
    stranger = create_user("nobody@example.test")

    assert client_for(stranger).get("/organization/workspaces/").status_code == 403
