"""The admin exception, and its boundary.

``Segment`` is registered because issue #3's acceptance criteria ask for it and
its end-user UI is two layers away; nothing else in this app is. The last test
here is what keeps that from quietly widening later.
"""

import pytest
from django.contrib import admin
from django.core.exceptions import ValidationError

from apps.contacts import services
from apps.contacts.models import Contact, ContactTag, CustomField, CustomFieldValue, Segment, Tag
from tests.support import TEST_PASSWORD, create_user

HOOKS = (
    "has_module_permission",
    "has_view_permission",
    "has_add_permission",
    "has_change_permission",
    "has_delete_permission",
)


@pytest.fixture
def segment_admin():
    return admin.site._registry[Segment]


@pytest.mark.django_db
class TestOnlySuperusersReachSegments:
    @pytest.mark.parametrize("hook", HOOKS)
    def test_every_permission_hook_refuses_a_non_superuser(self, rf, segment_admin, tenancy, hook):
        request = rf.get("/admin/")
        request.user = tenancy.owner

        assert getattr(segment_admin, hook)(request) is False

    @pytest.mark.parametrize("hook", HOOKS)
    def test_every_permission_hook_admits_a_superuser(self, rf, segment_admin, hook):
        request = rf.get("/admin/")
        request.user = create_user("root@example.test", is_staff=True, is_superuser=True)

        assert getattr(segment_admin, hook)(request) is True

    def test_a_staff_user_who_is_not_a_superuser_cannot_open_the_changelist(self, client, tenancy):
        """is_staff is what the admin already requires; it is not the bar for a
        cross-tenant view."""
        staff = create_user("staff@example.test", is_staff=True)
        client.login(email=staff.email, password=TEST_PASSWORD)

        response = client.get("/admin/contacts/segment/")

        assert response.status_code in {302, 403}

    def test_a_superuser_can_list_and_add(self, client, tenancy):
        create_user("root@example.test", is_staff=True, is_superuser=True)
        client.login(email="root@example.test", password=TEST_PASSWORD)

        assert client.get("/admin/contacts/segment/").status_code == 200
        assert client.get("/admin/contacts/segment/add/").status_code == 200


@pytest.mark.django_db
class TestFilterJsonIsValidatedOnSave:
    def test_an_invalid_filter_is_a_field_error_rather_than_a_500(self, tenancy):
        segment = Segment(workspace=tenancy.workspace, name="Broken", filter_json={"match": "sideways", "rules": []})

        with pytest.raises(ValidationError) as exc:
            segment.full_clean()

        assert "filter_json" in exc.value.message_dict

    def test_a_valid_filter_passes(self, tenancy):
        tag, _ = services.get_or_create_tag(tenancy.workspace, "VIP")
        segment = Segment(
            workspace=tenancy.workspace,
            name="VIPs",
            filter_json={"match": "all", "rules": [{"source": "tag", "key": str(tag.pk), "op": "has"}]},
        )

        segment.full_clean()

    def test_a_segment_with_no_workspace_yet_does_not_explode(self):
        """clean() runs before the workspace is bound on an admin add page."""
        Segment(name="Draft", filter_json={}).clean()


class TestNothingElseIsRegistered:
    @pytest.mark.parametrize("model", [Contact, Tag, ContactTag, CustomField, CustomFieldValue])
    def test_the_other_contacts_models_stay_out_of_the_admin(self, model):
        """apps/credentials/admin.py's rule, kept enforceable: a changelist over
        Contact is a cross-tenant PII browser, and Tag/CustomField already have
        permission-gated workspace UI in this issue."""
        assert model not in admin.site._registry
