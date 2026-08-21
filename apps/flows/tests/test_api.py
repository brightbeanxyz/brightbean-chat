"""The builder data API (SPEC §16): payloads, permissions, limits and CSRF.

The IDOR sweep in ``tests/idor.py`` reaches these routes through the *victim's*
``workspace_id``, where ``RBACMiddleware`` answers first. The sharper case is
here: the attacker's own workspace id paired with the victim's flow id, where
``get_scoped_object_or_404`` is the only thing standing in the way.
"""

import json

import pytest
from django.test import Client
from django.urls import reverse

from apps.flows.fixtures import graph_for
from apps.flows.models import FlowVersion
from apps.flows.schema import empty_graph, json_schema
from apps.flows.services import archive_flow, create_flow, latest_version, publish, save_draft
from apps.members.roles import WorkspaceRole

pytestmark = pytest.mark.django_db


def detail_url(tenancy, flow):
    return reverse("flows:api_detail", kwargs={"workspace_id": tenancy.workspace.pk, "flow_id": flow.pk})


def publish_url(tenancy, flow):
    return reverse("flows:api_publish", kwargs={"workspace_id": tenancy.workspace.pk, "flow_id": flow.pk})


def stats_url(tenancy, flow):
    return reverse("flows:api_stats", kwargs={"workspace_id": tenancy.workspace.pk, "flow_id": flow.pk})


def put(client, url, graph):
    return client.put(url, data=json.dumps({"graph": graph}), content_type="application/json")


@pytest.fixture
def flow(tenancy):
    return create_flow(workspace=tenancy.workspace, name="Welcome", user=tenancy.owner)


class TestRead:
    def test_it_returns_the_draft_metadata_and_graph(self, tenancy, client_for, flow):
        save_draft(flow, graph_for("send_message"), user=tenancy.owner)

        response = client_for(tenancy.owner).get(detail_url(tenancy, flow))
        payload = response.json()

        assert response.status_code == 200
        assert payload["flow"]["name"] == "Welcome"
        assert payload["version"]["version"] == 1
        assert payload["graph"] == graph_for("send_message")
        assert payload["published_version"] is None
        assert payload["validation"] == {"errors": [], "warnings": []}

    def test_it_reports_the_published_version_separately(self, tenancy, client_for, flow):
        save_draft(flow, graph_for("send_message"), user=tenancy.owner)
        publish(flow, user=tenancy.owner)

        payload = client_for(tenancy.owner).get(detail_url(tenancy, flow)).json()

        assert payload["published_version"]["version"] == 1
        assert payload["flow"]["status"] == "active"

    def test_it_reports_validation_findings_without_hiding_the_graph(self, tenancy, client_for, flow):
        save_draft(flow, empty_graph(), user=tenancy.owner)

        payload = client_for(tenancy.owner).get(detail_url(tenancy, flow)).json()

        assert [issue["code"] for issue in payload["validation"]["errors"]] == ["no_entry_node"]
        assert payload["graph"] == empty_graph()

    def test_it_carries_the_limits_and_the_schema_url(self, tenancy, client_for, flow):
        payload = client_for(tenancy.owner).get(detail_url(tenancy, flow)).json()

        assert payload["limits"]["max_nodes"] == 500
        assert payload["schema_url"] == reverse("flows:api_schema", kwargs={"workspace_id": tenancy.workspace.pk})

    def test_a_flow_with_no_version_still_returns_a_valid_envelope(self, tenancy, client_for, flow):
        """`{}` is not a graph: it fails the very schema this response links to,
        so the client got three envelope errors about a graph nobody wrote."""
        FlowVersion.objects.for_workspace(tenancy.workspace).filter(flow=flow).delete()

        payload = client_for(tenancy.owner).get(detail_url(tenancy, flow)).json()

        assert payload["version"] is None
        assert payload["graph"] == empty_graph()
        assert [issue["code"] for issue in payload["validation"]["errors"]] == ["no_entry_node"]


class TestPicklists:
    def test_every_key_is_always_present(self, tenancy, client_for, flow):
        """A client that has to branch on which keys exist is a client that
        breaks the day one of the stubs starts arriving."""
        payload = client_for(tenancy.owner).get(detail_url(tenancy, flow)).json()

        assert set(payload["picklists"]) == {
            "tags",
            "custom_fields",
            "sequences",
            "flows",
            "connections",
            "members",
        }

    def test_flows_and_members_are_real(self, tenancy, client_for, flow):
        payload = client_for(tenancy.owner).get(detail_url(tenancy, flow)).json()

        assert [entry["label"] for entry in payload["picklists"]["flows"]] == ["Welcome"]
        assert tenancy.owner.email in {entry["email"] for entry in payload["picklists"]["members"]}

    def test_the_unbuilt_apps_degrade_to_empty_lists(self, tenancy, client_for, flow):
        payload = client_for(tenancy.owner).get(detail_url(tenancy, flow)).json()

        assert payload["picklists"]["tags"] == []
        assert payload["picklists"]["custom_fields"] == []
        assert payload["picklists"]["connections"] == []
        assert payload["picklists"]["sequences"] == []

    def test_archived_flows_are_not_offered_as_start_flow_targets(self, tenancy, client_for, flow):
        archive_flow(create_flow(workspace=tenancy.workspace, name="Old"))

        payload = client_for(tenancy.owner).get(detail_url(tenancy, flow)).json()

        assert [entry["label"] for entry in payload["picklists"]["flows"]] == ["Welcome"]


class TestSave:
    def test_a_draft_round_trips(self, tenancy, client_for, flow):
        response = put(client_for(tenancy.owner), detail_url(tenancy, flow), graph_for("action"))

        assert response.status_code == 200
        assert response.json()["version"]["version"] == 1
        assert latest_version(flow).graph_json == graph_for("action")

    def test_a_half_wired_graph_saves_and_reports_the_problem(self, tenancy, client_for, flow):
        graph = graph_for("send_message")
        graph["edges"][0]["target"] = "nowhere"

        response = put(client_for(tenancy.owner), detail_url(tenancy, flow), graph)

        assert response.status_code == 200
        assert [issue["code"] for issue in response.json()["validation"]["errors"]] == ["dangling_edge"]
        assert latest_version(flow).graph_json == graph

    def test_an_unknown_config_key_is_refused_and_nothing_is_written(self, tenancy, client_for, flow):
        """The mass-assignment guard (SECURITY-BASELINE §7). Unlike a dangling
        edge, this one must never reach the column."""
        graph = graph_for("send_sms")
        graph["nodes"][0]["config"]["from_number"] = "+15550000"

        response = put(client_for(tenancy.owner), detail_url(tenancy, flow), graph)

        assert response.status_code == 422
        assert [issue["code"] for issue in response.json()["validation"]["errors"]] == ["unknown_config_key"]
        assert latest_version(flow).graph_json == empty_graph()

    def test_an_oversized_body_is_refused_before_it_is_parsed(self, tenancy, client_for, flow):
        response = client_for(tenancy.owner).put(
            detail_url(tenancy, flow),
            data=json.dumps({"graph": {"schema": 1, "nodes": [], "edges": [], "pad": "x" * 600_000}}),
            content_type="application/json",
        )

        assert response.status_code == 413

    def test_malformed_json_is_a_400(self, tenancy, client_for, flow):
        response = client_for(tenancy.owner).put(
            detail_url(tenancy, flow), data="{not json", content_type="application/json"
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "malformed_json"

    def test_a_body_without_a_graph_is_a_400(self, tenancy, client_for, flow):
        response = client_for(tenancy.owner).put(
            detail_url(tenancy, flow), data=json.dumps({"name": "nope"}), content_type="application/json"
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "missing_graph"


class TestPublishEndpoint:
    def test_it_publishes_a_valid_draft(self, tenancy, client_for, flow):
        save_draft(flow, graph_for("send_message"), user=tenancy.owner)

        response = client_for(tenancy.owner).post(publish_url(tenancy, flow))

        assert response.status_code == 200
        assert response.json()["version"]["published"] is True
        assert response.json()["flow"]["status"] == "active"

    def test_errors_answer_422_with_the_same_shape_the_save_returns(self, tenancy, client_for, flow):
        response = client_for(tenancy.owner).post(publish_url(tenancy, flow))

        assert response.status_code == 422
        assert [issue["code"] for issue in response.json()["validation"]["errors"]] == ["no_entry_node"]
        assert FlowVersion.objects.for_workspace(tenancy.workspace).filter(published=True).count() == 0

    def test_a_get_is_not_a_publish(self, tenancy, client_for, flow):
        assert client_for(tenancy.owner).get(publish_url(tenancy, flow)).status_code == 405


class TestStats:
    def test_it_is_a_documented_zeros_stub(self, tenancy, client_for, flow):
        payload = client_for(tenancy.owner).get(stats_url(tenancy, flow)).json()

        assert payload["available"] is False
        assert payload["nodes"] == {}
        assert payload["totals"] == {"sent": 0, "delivered": 0, "failed": 0, "clicked": 0}


class TestSchemaEndpoint:
    def test_it_serves_the_generated_document(self, tenancy, client_for):
        url = reverse("flows:api_schema", kwargs={"workspace_id": tenancy.workspace.pk})

        response = client_for(tenancy.owner).get(url)

        assert response.status_code == 200
        assert response.json() == json_schema()


class TestPermissions:
    @pytest.mark.parametrize("role", [WorkspaceRole.ADMIN, WorkspaceRole.EDITOR])
    def test_editors_and_admins_may_write(self, tenancy, client_for, flow, role):
        client = client_for(tenancy.user_for(role))

        assert put(client, detail_url(tenancy, flow), graph_for("send_message")).status_code == 200
        assert client.post(publish_url(tenancy, flow)).status_code == 200

    @pytest.mark.parametrize("role", [WorkspaceRole.AGENT, WorkspaceRole.VIEWER])
    def test_agents_and_viewers_are_read_only(self, tenancy, client_for, flow, role):
        client = client_for(tenancy.user_for(role))

        assert client.get(detail_url(tenancy, flow)).status_code == 200
        assert client.get(stats_url(tenancy, flow)).status_code == 200
        assert put(client, detail_url(tenancy, flow), graph_for("send_message")).status_code == 403
        assert client.post(publish_url(tenancy, flow)).status_code == 403

    def test_a_read_by_a_viewer_does_not_change_the_draft(self, tenancy, client_for, flow):
        client_for(tenancy.user_for(WorkspaceRole.VIEWER)).get(detail_url(tenancy, flow))

        assert latest_version(flow).graph_json == empty_graph()

    def test_anonymous_callers_are_sent_to_the_login_page(self, tenancy, flow):
        response = Client().get(detail_url(tenancy, flow))

        assert response.status_code == 302


class TestCsrf:
    def test_a_write_without_the_header_is_refused(self, tenancy, flow):
        """SECURITY-BASELINE §8: CSRF is enforced on session-authenticated
        endpoints, the builder data API included. No csrf_exempt anywhere."""
        client = Client(enforce_csrf_checks=True)
        client.force_login(tenancy.owner)

        response = put(client, detail_url(tenancy, flow), graph_for("send_message"))

        assert response.status_code == 403

    def test_a_write_with_the_header_goes_through(self, tenancy, flow):
        client = Client(enforce_csrf_checks=True)
        client.force_login(tenancy.owner)
        client.get(reverse("flows:edit", kwargs={"workspace_id": tenancy.workspace.pk, "flow_id": flow.pk}))
        token = client.cookies["csrftoken"].value

        response = client.put(
            detail_url(tenancy, flow),
            data=json.dumps({"graph": graph_for("send_message")}),
            content_type="application/json",
            headers={"x-csrftoken": token},
        )

        assert response.status_code == 200


class TestTenantIsolation:
    def test_another_workspaces_flow_is_not_reachable_through_your_own(self, tenancy, other_tenancy, client_for):
        """The case the sweep cannot make: the attacker uses their *own*
        workspace id, so the middleware is satisfied and only the scoped lookup
        stands between them and someone else's flow."""
        victim_flow = create_flow(workspace=tenancy.workspace, name="Victim")
        url = reverse(
            "flows:api_detail",
            kwargs={"workspace_id": other_tenancy.workspace.pk, "flow_id": victim_flow.pk},
        )
        client = client_for(other_tenancy.owner)

        assert client.get(url).status_code == 404
        assert put(client, url, graph_for("send_message")).status_code == 404

    def test_the_same_holds_for_publish_and_stats(self, tenancy, other_tenancy, client_for):
        victim_flow = create_flow(workspace=tenancy.workspace, name="Victim")
        keys = {"workspace_id": other_tenancy.workspace.pk, "flow_id": victim_flow.pk}
        client = client_for(other_tenancy.owner)

        assert client.post(reverse("flows:api_publish", kwargs=keys)).status_code == 404
        assert client.get(reverse("flows:api_stats", kwargs=keys)).status_code == 404

    def test_a_flow_that_does_not_exist_answers_the_same_404(self, tenancy, client_for):
        url = reverse(
            "flows:api_detail",
            kwargs={"workspace_id": tenancy.workspace.pk, "flow_id": "0192f000-0000-7000-8000-00000000dead"},
        )

        assert client_for(tenancy.owner).get(url).status_code == 404


class TestHostileBodies:
    def test_a_malformed_content_length_is_a_400_not_a_500(self, tenancy, client_for, flow):
        """Django re-parses the header inside request.body and lets the
        ValueError escape, so an unguarded view answers 500 to a header of
        "twelve"."""
        response = client_for(tenancy.owner).put(
            detail_url(tenancy, flow),
            data=json.dumps({"graph": empty_graph()}),
            content_type="application/json",
            headers={"content-length": "not-a-number"},
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "malformed_request"

    def test_a_deeply_nested_body_is_stopped_by_the_depth_cap(self, tenancy, client_for, flow):
        """A document built to exhaust a parser gets no further than the depth
        cap, and nothing is written (SECURITY-BASELINE §7)."""
        payload = '{"graph":' + "[" * 5000 + "]" * 5000 + "}"

        response = client_for(tenancy.owner).put(
            detail_url(tenancy, flow), data=payload, content_type="application/json"
        )

        assert response.status_code == 422
        assert [issue["code"] for issue in response.json()["validation"]["errors"]] == ["graph_too_deep"]
        assert latest_version(flow).graph_json == empty_graph()

    def test_a_graph_that_is_not_an_object_is_refused(self, tenancy, client_for, flow):
        response = put(client_for(tenancy.owner), detail_url(tenancy, flow), ["not", "a", "graph"])

        assert response.status_code == 422
        assert [issue["code"] for issue in response.json()["validation"]["errors"]] == ["graph_not_object"]

    def test_a_non_finite_number_is_refused_rather_than_reaching_postgres(self, tenancy, client_for, flow):
        """CPython's decoder accepts bare NaN and Infinity; jsonb does not, so
        without this the save is a 500 on an authenticated PUT."""
        graph = graph_for("note")
        body = json.dumps({"graph": graph}).replace('"x": 0', '"x": NaN', 1)
        assert "NaN" in body

        response = client_for(tenancy.owner).put(detail_url(tenancy, flow), data=body, content_type="application/json")

        assert response.status_code == 422
        assert [issue["code"] for issue in response.json()["validation"]["errors"]] == ["non_finite_number"]
        assert latest_version(flow).graph_json == empty_graph()

    def test_an_overflowing_float_is_refused_too(self, tenancy, client_for, flow):
        graph = graph_for("note")
        body = json.dumps({"graph": graph}).replace('"x": 0', '"x": 1e999', 1)

        response = client_for(tenancy.owner).put(detail_url(tenancy, flow), data=body, content_type="application/json")

        assert response.status_code == 422
        assert latest_version(flow).graph_json == empty_graph()

    def test_a_json_array_body_is_refused(self, tenancy, client_for, flow):
        response = client_for(tenancy.owner).put(
            detail_url(tenancy, flow), data="[1, 2, 3]", content_type="application/json"
        )

        assert response.status_code == 400
