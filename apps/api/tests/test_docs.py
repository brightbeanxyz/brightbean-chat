"""``/api/v1/docs`` and ``/api/v1/openapi.json``.

The docs page is generated from the OpenAPI schema on purpose — a hand-written
endpoint table is a table that is wrong within two releases. So the test worth
having is that the page really is derived from the routes, not that it contains
a particular sentence.
"""

import json

import pytest


@pytest.mark.django_db
class TestOpenApi:
    def test_the_schema_is_served_without_a_key(self, client):
        """It describes the API's shape and reads nothing.

        Requiring a credential to read the reference for a credential-based API
        only makes integrating harder.
        """
        response = client.get("/api/v1/openapi.json")

        assert response.status_code == 200
        schema = json.loads(response.content)
        assert schema["info"]["title"] == "BrightBean Chat API"
        assert schema["info"]["version"] == "1.0.0"

    def test_it_lists_every_documented_endpoint(self, client):
        schema = json.loads(client.get("/api/v1/openapi.json").content)
        paths = schema["paths"]

        assert "/api/v1/contacts" in paths
        assert "/api/v1/contacts/{contact_id}" in paths
        assert "/api/v1/contacts/{contact_id}/tags" in paths
        assert "/api/v1/contacts/{contact_id}/tags/{tag_id}" in paths
        assert "/api/v1/contacts/{contact_id}/fields/{field_id}" in paths
        assert "/api/v1/contacts/{contact_id}/flows/{flow_id}/start" in paths
        assert "/api/v1/messages" in paths
        assert "/api/v1/flows" in paths
        assert "/api/v1/tags" in paths
        assert "/api/v1/fields" in paths

    def test_every_operation_declares_bearer_security(self, client):
        """ "No anonymous surface" — asserted against the generated document.

        Global auth means a route cannot forget its own; this is what would
        notice if someone reintroduced a per-operation ``auth=None``.
        """
        schema = json.loads(client.get("/api/v1/openapi.json").content)

        for path, operations in schema["paths"].items():
            for method, operation in operations.items():
                assert operation.get("security"), f"{method.upper()} {path} has no security requirement"


@pytest.mark.django_db
class TestDocsPage:
    def test_it_renders_for_an_anonymous_visitor(self, client):
        response = client.get("/api/v1/docs")

        assert response.status_code == 200

    def test_the_endpoint_table_comes_from_the_routes(self, client):
        body = client.get("/api/v1/docs").content.decode()

        assert "/api/v1/contacts/{contact_id}/flows/{flow_id}/start" in body
        assert "PATCH" in body

    def test_it_documents_the_wire_contract_integrators_copy(self, client):
        body = client.get("/api/v1/docs").content.decode()

        assert "X-BrightBean-Signature" in body
        assert "X-BrightBean-Timestamp" in body
        assert "Authorization: Bearer bb_" in body
        assert "compliance_denied" in body
        assert "next_cursor" in body

    def test_it_loads_no_third_party_script(self, client):
        """SECURITY-BASELINE §8: nonce-based CSP, so no CDN.

        This is why the page exists at all instead of Ninja's built-in Swagger
        console — see apps/api/views_docs.py.
        """
        body = client.get("/api/v1/docs").content.decode()

        assert "cdn.jsdelivr.net" not in body
        assert "unpkg.com" not in body
        assert "swagger" not in body.lower()
