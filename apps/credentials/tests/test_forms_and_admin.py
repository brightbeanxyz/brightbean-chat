"""The admin form override, and why it is not cosmetic."""

import pytest
from django.contrib.admin.sites import AdminSite
from django.test import RequestFactory

from apps.credentials.admin import PlatformCredentialAdmin
from apps.credentials.forms import PlatformCredentialAdminForm, WorkspaceCredentialOverrideForm
from apps.credentials.models import CONFIGURABLE_PLATFORMS, PlatformCredential, WorkspaceCredentialOverride
from tests.support import create_user

COMPLETE = {"client_id": "id-12345", "client_secret": "secret-67890"}


@pytest.mark.django_db
class TestNoEditSaveDoesNotCorruptTheRow:
    """Without the forms.JSONField override, the auto-generated CharField
    renders a Python repr (single quotes, not JSON), cleans it back to a str,
    and get_prep_value json.dumps()es *that* into a JSON string literal. The
    next read returns a str where every consumer expects a mapping. Opening the
    change page and pressing Save with no edits is enough to do it."""

    def test_the_round_trip_keeps_a_dict(self, tenancy):
        credential = PlatformCredential.objects.create(
            organization=tenancy.organization, platform="instagram", credentials=COMPLETE
        )

        form = PlatformCredentialAdminForm(instance=credential)
        rendered = form.initial["credentials"]
        posted = {
            "organization": str(tenancy.organization.pk),
            "platform": "instagram",
            "credentials": rendered if isinstance(rendered, str) else __import__("json").dumps(rendered),
        }
        bound = PlatformCredentialAdminForm(posted, instance=credential)

        assert bound.is_valid(), bound.errors
        bound.save()
        credential.refresh_from_db()
        assert credential.credentials == COMPLETE
        assert credential.is_configured is True

    def test_the_field_is_a_real_json_field(self):
        from django import forms

        assert isinstance(PlatformCredentialAdminForm.base_fields["credentials"], forms.JSONField)


@pytest.mark.django_db
class TestFormValidation:
    def test_an_incomplete_set_is_rejected_with_the_expected_keys(self, tenancy):
        form = PlatformCredentialAdminForm(
            {
                "organization": str(tenancy.organization.pk),
                "platform": "instagram",
                "credentials": '{"client_id": "only-this"}',
            }
        )

        assert not form.is_valid()
        assert "client_secret" in str(form.errors["credentials"])

    def test_a_non_object_is_rejected(self, tenancy):
        form = PlatformCredentialAdminForm(
            {"organization": str(tenancy.organization.pk), "platform": "instagram", "credentials": '["a", "b"]'}
        )

        assert not form.is_valid()

    def test_values_are_coerced_to_strings(self, tenancy):
        form = PlatformCredentialAdminForm(
            {
                "organization": str(tenancy.organization.pk),
                "platform": "instagram",
                "credentials": '{"client_id": 12345, "client_secret": "s"}',
            }
        )

        assert form.is_valid(), form.errors
        assert form.cleaned_data["credentials"]["client_id"] == "12345"

    def test_blank_values_are_dropped(self, tenancy):
        form = PlatformCredentialAdminForm(
            {
                "organization": str(tenancy.organization.pk),
                "platform": "instagram",
                "credentials": '{"client_id": "a", "client_secret": "b", "spare": "  "}',
            }
        )

        assert form.is_valid(), form.errors
        assert "spare" not in form.cleaned_data["credentials"]

    def test_only_platforms_with_app_credentials_are_offered(self, tenancy):
        form = PlatformCredentialAdminForm()
        offered = {value for value, _ in form.fields["platform"].choices if value}

        assert offered == set(CONFIGURABLE_PLATFORMS)
        assert "telegram" not in offered


@pytest.mark.django_db
class TestWorkspaceOverrideForm:
    def test_it_validates_completeness_against_the_url_platform(self, tenancy):
        instance = WorkspaceCredentialOverride(workspace=tenancy.workspace, platform="instagram")
        form = WorkspaceCredentialOverrideForm(
            {"credentials": '{"client_id": "a"}'}, instance=instance, platform="instagram"
        )

        assert not form.is_valid()

    def test_a_complete_set_saves(self, tenancy):
        import json

        instance = WorkspaceCredentialOverride(workspace=tenancy.workspace, platform="instagram")
        form = WorkspaceCredentialOverrideForm(
            {"credentials": json.dumps(COMPLETE)}, instance=instance, platform="instagram"
        )

        assert form.is_valid(), form.errors
        saved = form.save()
        assert saved.is_configured is True


@pytest.mark.django_db
class TestAdminIsSuperuserOnly:
    """Opening the change page decrypts secrets into an HTML response, so
    is_staff — which the admin already requires — is not a high enough bar."""

    @pytest.fixture
    def admin(self):
        return PlatformCredentialAdmin(PlatformCredential, AdminSite())

    @pytest.mark.parametrize(
        "hook",
        [
            "has_module_permission",
            "has_view_permission",
            "has_add_permission",
            "has_change_permission",
            "has_delete_permission",
        ],
    )
    def test_staff_without_superuser_is_refused(self, admin, hook):
        request = RequestFactory().get("/admin/")
        request.user = create_user("staff@example.test", is_staff=True)

        assert getattr(admin, hook)(request) is False

    @pytest.mark.parametrize(
        "hook",
        [
            "has_module_permission",
            "has_view_permission",
            "has_add_permission",
            "has_change_permission",
            "has_delete_permission",
        ],
    )
    def test_a_superuser_is_allowed(self, admin, hook):
        request = RequestFactory().get("/admin/")
        request.user = create_user("root@example.test", is_staff=True, is_superuser=True)

        assert getattr(admin, hook)(request) is True

    def test_workspace_overrides_are_not_registered_in_the_admin(self):
        """They are tenant data with their own permission-gated UI; an admin
        listing would be a cross-tenant view of every workspace's secrets."""
        from django.contrib import admin as django_admin

        assert WorkspaceCredentialOverride not in django_admin.site._registry
