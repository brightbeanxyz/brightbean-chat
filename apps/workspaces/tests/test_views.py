"""Workspace dashboard, switcher and settings."""

import pytest

from apps.members.models import WorkspaceMembership
from apps.workspaces.models import Workspace


@pytest.mark.django_db
class TestSwitcher:
    def test_switching_records_the_workspace(self, tenancy, client_for):
        second = Workspace.objects.create(organization=tenancy.organization, name="second")
        WorkspaceMembership.objects.create(user=tenancy.owner, workspace=second, workspace_role="admin")
        client = client_for(tenancy.owner)

        response = client.post(f"/w/{second.pk}/switch/")

        assert response.status_code == 302
        tenancy.owner.refresh_from_db()
        assert tenancy.owner.last_workspace_id == second.pk

    def test_it_refuses_get(self, tenancy, client_for):
        """Studio uses a GET link; this writes state, so it is CSRF-exposed and
        prefetchable as a GET."""
        response = client_for(tenancy.owner).get(f"/w/{tenancy.workspace.pk}/switch/")

        assert response.status_code == 405

    def test_a_workspace_you_are_not_in_is_a_404(self, tenancy, other_tenancy, client_for):
        response = client_for(other_tenancy.owner).post(f"/w/{tenancy.workspace.pk}/switch/")

        assert response.status_code == 404


@pytest.mark.django_db
class TestWorkspaceSettings:
    def test_an_admin_can_rename_the_workspace(self, tenancy, client_for):
        client = client_for(tenancy.user_for("admin"))

        client.post(f"/w/{tenancy.workspace.pk}/settings/update/", {"name": "Renamed"})

        tenancy.workspace.refresh_from_db()
        assert tenancy.workspace.name == "Renamed"

    def test_an_invalid_colour_is_rejected(self, tenancy, client_for):
        client = client_for(tenancy.user_for("admin"))

        client.post(
            f"/w/{tenancy.workspace.pk}/settings/update/",
            {"name": "Keep", "primary_color": "octarine"},
        )

        tenancy.workspace.refresh_from_db()
        assert tenancy.workspace.primary_color == ""

    def test_a_valid_colour_is_stored(self, tenancy, client_for):
        client = client_for(tenancy.user_for("admin"))

        client.post(
            f"/w/{tenancy.workspace.pk}/settings/update/",
            {"name": "Keep", "primary_color": "#F97316"},
        )

        tenancy.workspace.refresh_from_db()
        assert tenancy.workspace.primary_color == "#F97316"


@pytest.mark.django_db
class TestWorkspaceModel:
    def test_effective_timezone_falls_back_to_the_organization(self, tenancy):
        tenancy.organization.default_timezone = "Europe/Berlin"
        tenancy.organization.save(update_fields=["default_timezone"])

        assert tenancy.workspace.effective_timezone == "Europe/Berlin"

    def test_its_own_timezone_wins(self, tenancy):
        tenancy.workspace.timezone = "America/New_York"

        assert tenancy.workspace.effective_timezone == "America/New_York"

    def test_names_are_unique_within_an_organization(self, tenancy):
        from django.db import IntegrityError

        with pytest.raises(IntegrityError):
            Workspace.objects.create(organization=tenancy.organization, name=tenancy.workspace.name)

    def test_the_same_name_is_fine_in_another_organization(self, tenancy, other_tenancy):
        Workspace.objects.create(organization=other_tenancy.organization, name=tenancy.workspace.name)

    def test_studio_only_fields_are_not_ported(self):
        """Deviation 9: approval workflows and posting defaults are Studio's
        social-publishing product, not this one."""
        fields = {field.name for field in Workspace._meta.get_fields()}

        assert "approval_workflow_mode" not in fields
        assert "default_hashtags" not in fields
        assert "default_first_comment" not in fields
