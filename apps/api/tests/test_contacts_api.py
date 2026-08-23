"""``/api/v1/contacts`` — the shape, the filters, and the facade discipline.

The load-bearing assertion in this file is not that a contact comes back with
the right keys. It is that **every write emits the contract-7 event**, because
that is the whole reason ROADMAP contract 1 forbids the ORM here: an API that
wrote model fields directly would update the CRM and deliver no webhooks, and
the symptom would show up months later as "webhooks are unreliable".
"""

import json

import pytest
from django.dispatch import Signal

from apps.contacts.events import (
    EVENT_CATALOG,
    EVENT_CONTACT_CREATED,
    EVENT_CONTACT_FIELD_CHANGED,
    EVENT_CONTACT_TAG_ADDED,
    EVENT_CONTACT_TAG_REMOVED,
)
from apps.contacts.models import Contact, CustomField, CustomFieldType, Tag

CONTACTS = "/api/v1/contacts"


@pytest.fixture
def caught():
    """Record every contacts-catalog event fired inside the block.

    Bound to the real signals rather than to a patched service, so a route that
    somehow bypassed the facade would show up as an empty list here.
    """
    seen: list[dict] = []

    def receiver(sender, **payload):
        payload.pop("signal", None)
        seen.append(payload)

    signals: list[Signal] = list(EVENT_CATALOG.values())
    for signal in signals:
        signal.connect(receiver, weak=False)
    try:
        yield seen
    finally:
        for signal in signals:
            signal.disconnect(receiver)


def post(client, url, payload, auth):
    return client.post(url, data=json.dumps(payload), content_type="application/json", **auth)


@pytest.mark.django_db
class TestListing:
    def test_an_empty_workspace_lists_nothing(self, client, tenancy, auth):
        response = client.get(CONTACTS, **auth)

        assert response.status_code == 200
        assert response.json() == {"data": [], "has_more": False, "next_cursor": None}

    def test_it_returns_the_documented_shape(self, client, tenancy, auth):
        contact = Contact.objects.create(workspace=tenancy.workspace, first_name="Ada", email="ada@example.com")
        tag = Tag.objects.create(workspace=tenancy.workspace, name="vip")
        from apps.contacts.services import add_tag

        add_tag(contact, tag)

        row = client.get(CONTACTS, **auth).json()["data"][0]

        assert row["id"] == str(contact.pk)
        assert row["first_name"] == "Ada"
        assert row["email"] == "ada@example.com"
        assert row["status"] == "active"
        assert row["tags"] == [{"id": str(tag.pk), "name": "vip"}]
        assert set(row) == {
            "id",
            "first_name",
            "last_name",
            "email",
            "phone",
            "locale",
            "timezone",
            "status",
            "tags",
            "last_interaction_at",
            "created_at",
            "updated_at",
        }

    def test_another_workspaces_contacts_are_invisible(self, client, tenancy, other_tenancy, auth):
        Contact.objects.create(workspace=other_tenancy.workspace, first_name="Stranger")

        assert client.get(CONTACTS, **auth).json()["data"] == []

    def test_search_matches_the_four_columns_the_crm_searches(self, client, tenancy, auth):
        Contact.objects.create(workspace=tenancy.workspace, first_name="Ada", email="ada@example.com")
        Contact.objects.create(workspace=tenancy.workspace, first_name="Grace", phone="+15550001")

        assert len(client.get(f"{CONTACTS}?q=ada", **auth).json()["data"]) == 1
        assert len(client.get(f"{CONTACTS}?q=5550001", **auth).json()["data"]) == 1
        assert len(client.get(f"{CONTACTS}?q=nobody", **auth).json()["data"]) == 0

    def test_tag_filter(self, client, tenancy, auth):
        from apps.contacts.services import add_tag

        tagged = Contact.objects.create(workspace=tenancy.workspace, first_name="Tagged")
        Contact.objects.create(workspace=tenancy.workspace, first_name="Untagged")
        tag = Tag.objects.create(workspace=tenancy.workspace, name="vip")
        add_tag(tagged, tag)

        rows = client.get(f"{CONTACTS}?tag_id={tag.pk}", **auth).json()["data"]

        assert [row["first_name"] for row in rows] == ["Tagged"]

    def test_a_bad_status_is_a_422_not_an_empty_list(self, client, tenancy, auth):
        response = client.get(f"{CONTACTS}?status=banana", **auth)

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_request"

    def test_deleted_contacts_are_hidden_by_default(self, client, tenancy, auth):
        from apps.contacts.services import delete_contact

        contact = Contact.objects.create(workspace=tenancy.workspace, first_name="Gone")
        delete_contact(contact)

        assert client.get(CONTACTS, **auth).json()["data"] == []
        assert len(client.get(f"{CONTACTS}?status=deleted", **auth).json()["data"]) == 1


@pytest.mark.django_db
class TestPagination:
    def test_it_pages_forward_with_the_cursor(self, client, tenancy, auth):
        for index in range(5):
            Contact.objects.create(workspace=tenancy.workspace, first_name=f"C{index}")

        first = client.get(f"{CONTACTS}?limit=2", **auth).json()
        assert len(first["data"]) == 2
        assert first["has_more"] is True

        second = client.get(f"{CONTACTS}?limit=2&cursor={first['next_cursor']}", **auth).json()
        assert len(second["data"]) == 2
        assert {row["id"] for row in first["data"]} & {row["id"] for row in second["data"]} == set()

        third = client.get(f"{CONTACTS}?limit=2&cursor={second['next_cursor']}", **auth).json()
        assert len(third["data"]) == 1
        assert third["has_more"] is False
        assert third["next_cursor"] is None

    def test_the_limit_is_clamped_rather_than_refused(self, client, tenancy, auth):
        response = client.get(f"{CONTACTS}?limit=100000", **auth)

        assert response.status_code == 200

    def test_a_forged_cursor_is_a_422_not_a_500(self, client, tenancy, auth):
        response = client.get(f"{CONTACTS}?cursor=%%%not-base64", **auth)

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_cursor"


@pytest.mark.django_db
class TestWrites:
    def test_create_goes_through_the_facade_and_emits_the_event(self, client, tenancy, auth, caught):
        response = post(client, CONTACTS, {"first_name": "Ada", "email": "ada@example.com"}, auth)

        assert response.status_code == 201
        body = response.json()
        assert body["first_name"] == "Ada"

        contact = Contact.objects.for_workspace(tenancy.workspace).get(pk=body["id"])
        assert contact.email == "ada@example.com"
        created = [event for event in caught if event["event"] == EVENT_CONTACT_CREATED]
        assert len(created) == 1
        # SPEC §5's consent audit: an object created through this API says so.
        assert created[0]["source"] == "api"

    def test_create_rejects_unknown_keys(self, client, tenancy, auth):
        """SECURITY-BASELINE §7's mass-assignment guard.

        ``workspace`` is the field that matters: a caller must not be able to
        steer a create at another tenant by adding a key nobody validated.
        """
        response = post(client, CONTACTS, {"first_name": "Ada", "workspace": "whatever"}, auth)

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_request"

    def test_patch_updates_only_what_it_names(self, client, tenancy, auth):
        contact = Contact.objects.create(workspace=tenancy.workspace, first_name="Ada", last_name="Lovelace")

        response = client.patch(
            f"{CONTACTS}/{contact.pk}",
            data=json.dumps({"last_name": "Byron"}),
            content_type="application/json",
            **auth,
        )

        assert response.status_code == 200
        contact.refresh_from_db()
        assert (contact.first_name, contact.last_name) == ("Ada", "Byron")

    def test_patch_can_clear_a_field_with_an_empty_string(self, client, tenancy, auth):
        contact = Contact.objects.create(workspace=tenancy.workspace, first_name="Ada", email="ada@example.com")

        client.patch(
            f"{CONTACTS}/{contact.pk}",
            data=json.dumps({"email": ""}),
            content_type="application/json",
            **auth,
        )

        contact.refresh_from_db()
        assert contact.email == ""

    def test_adding_a_tag_by_name_creates_it_and_emits_the_event(self, client, tenancy, auth, caught):
        contact = Contact.objects.create(workspace=tenancy.workspace, first_name="Ada")

        response = post(client, f"{CONTACTS}/{contact.pk}/tags", {"name": "vip"}, auth)

        assert response.status_code == 201
        assert response.json()["name"] == "vip"
        assert Tag.objects.for_workspace(tenancy.workspace).filter(name="vip").exists()
        assert [event["event"] for event in caught if event["event"] == EVENT_CONTACT_TAG_ADDED]

    def test_adding_a_tag_twice_is_a_200_not_a_second_event(self, client, tenancy, auth, caught):
        contact = Contact.objects.create(workspace=tenancy.workspace, first_name="Ada")
        post(client, f"{CONTACTS}/{contact.pk}/tags", {"name": "vip"}, auth)
        caught.clear()

        response = post(client, f"{CONTACTS}/{contact.pk}/tags", {"name": "vip"}, auth)

        assert response.status_code == 200
        assert caught == []

    def test_adding_a_tag_needs_exactly_one_of_name_or_id(self, client, tenancy, auth):
        contact = Contact.objects.create(workspace=tenancy.workspace, first_name="Ada")

        assert post(client, f"{CONTACTS}/{contact.pk}/tags", {}, auth).status_code == 422
        tag = Tag.objects.create(workspace=tenancy.workspace, name="vip")
        both = post(client, f"{CONTACTS}/{contact.pk}/tags", {"name": "x", "tag_id": str(tag.pk)}, auth)
        assert both.status_code == 422

    def test_removing_a_tag_emits_the_event_and_is_idempotent(self, client, tenancy, auth, caught):
        from apps.contacts.services import add_tag

        contact = Contact.objects.create(workspace=tenancy.workspace, first_name="Ada")
        tag = Tag.objects.create(workspace=tenancy.workspace, name="vip")
        add_tag(contact, tag)
        caught.clear()

        first = client.delete(f"{CONTACTS}/{contact.pk}/tags/{tag.pk}", **auth)
        second = client.delete(f"{CONTACTS}/{contact.pk}/tags/{tag.pk}", **auth)

        assert first.status_code == 204
        assert second.status_code == 204
        assert len([event for event in caught if event["event"] == EVENT_CONTACT_TAG_REMOVED]) == 1

    def test_setting_a_field_goes_through_coerce_value(self, client, tenancy, auth, caught):
        contact = Contact.objects.create(workspace=tenancy.workspace, first_name="Ada")
        field = CustomField.objects.create(workspace=tenancy.workspace, name="Score", type=CustomFieldType.NUMBER)

        response = client.put(
            f"{CONTACTS}/{contact.pk}/fields/{field.pk}",
            data=json.dumps({"value": 42}),
            content_type="application/json",
            **auth,
        )

        assert response.status_code == 200
        assert response.json() == {"field_id": str(field.pk), "name": "Score", "type": "number", "value": 42}
        assert [event for event in caught if event["event"] == EVENT_CONTACT_FIELD_CHANGED]

    def test_a_value_of_the_wrong_type_is_a_422_naming_the_field_not_the_value(self, client, tenancy, auth):
        contact = Contact.objects.create(workspace=tenancy.workspace, first_name="Ada")
        field = CustomField.objects.create(workspace=tenancy.workspace, name="Score", type=CustomFieldType.NUMBER)

        response = client.put(
            f"{CONTACTS}/{contact.pk}/fields/{field.pk}",
            data=json.dumps({"value": "not a number"}),
            content_type="application/json",
            **auth,
        )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_field_value"

    def test_a_null_clears_the_field(self, client, tenancy, auth):
        from apps.contacts.services import set_field_value

        contact = Contact.objects.create(workspace=tenancy.workspace, first_name="Ada")
        field = CustomField.objects.create(workspace=tenancy.workspace, name="Note", type=CustomFieldType.TEXT)
        set_field_value(contact, field, "hello")

        response = client.put(
            f"{CONTACTS}/{contact.pk}/fields/{field.pk}",
            data=json.dumps({"value": None}),
            content_type="application/json",
            **auth,
        )

        assert response.status_code == 200
        assert response.json()["value"] is None
        assert client.get(f"{CONTACTS}/{contact.pk}/fields", **auth).json() == []


@pytest.mark.django_db
class TestNotFound:
    def test_an_unknown_contact_is_a_404_in_the_standard_envelope(self, client, tenancy, auth):
        response = client.get(f"{CONTACTS}/00000000-0000-7000-8000-000000000000", **auth)

        assert response.status_code == 404
        assert response.json() == {"error": {"code": "not_found", "message": "No such object.", "detail": {}}}

    def test_a_malformed_uuid_is_a_422_rather_than_a_500(self, client, tenancy, auth):
        response = client.get(f"{CONTACTS}/not-a-uuid", **auth)

        assert response.status_code in {404, 422}
