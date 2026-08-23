"""``GET /api/v1/flows``, ``/tags`` and ``/fields``.

Read-only, and the only interesting property is tenancy: every one of these is a
list of a workspace's own vocabulary, and a key must never see another
workspace's.
"""

import pytest

from apps.api.tests.conftest import bearer, make_key
from apps.contacts.models import CustomField, CustomFieldType, Tag
from apps.flows.services import create_flow


@pytest.mark.django_db
class TestCatalog:
    def test_tags_lists_only_this_workspaces(self, client, tenancy, other_tenancy, auth):
        Tag.objects.create(workspace=tenancy.workspace, name="mine")
        Tag.objects.create(workspace=other_tenancy.workspace, name="theirs")

        rows = client.get("/api/v1/tags", **auth).json()["data"]

        assert [row["name"] for row in rows] == ["mine"]

    def test_fields_lists_only_this_workspaces(self, client, tenancy, other_tenancy, auth):
        CustomField.objects.create(workspace=tenancy.workspace, name="Score", type=CustomFieldType.NUMBER)
        CustomField.objects.create(workspace=other_tenancy.workspace, name="Secret", type=CustomFieldType.TEXT)

        rows = client.get("/api/v1/fields", **auth).json()["data"]

        assert [row["name"] for row in rows] == ["Score"]
        assert rows[0]["type"] == "number"

    def test_flows_lists_only_this_workspaces(self, client, tenancy, other_tenancy, auth):
        create_flow(workspace=tenancy.workspace, name="Mine")
        create_flow(workspace=other_tenancy.workspace, name="Theirs")

        rows = client.get("/api/v1/flows", **auth).json()["data"]

        assert [row["name"] for row in rows] == ["Mine"]
        assert rows[0]["status"] == "draft"

    def test_flows_can_be_filtered_by_status(self, client, tenancy, auth):
        create_flow(workspace=tenancy.workspace, name="Draft one")

        assert len(client.get("/api/v1/flows?status=draft", **auth).json()["data"]) == 1
        assert len(client.get("/api/v1/flows?status=active", **auth).json()["data"]) == 0

    def test_an_unknown_status_is_a_422(self, client, tenancy, auth):
        response = client.get("/api/v1/flows?status=banana", **auth)

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_request"

    def test_a_read_scoped_key_can_reach_all_three(self, client, tenancy):
        _, plaintext = make_key(tenancy.workspace, scopes=("read",))

        for path in ("/api/v1/flows", "/api/v1/tags", "/api/v1/fields"):
            assert client.get(path, **bearer(plaintext)).status_code == 200
