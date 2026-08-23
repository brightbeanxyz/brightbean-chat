"""``POST /api/v1/contacts/<id>/flows/<flow_id>/start``.

The endpoint SPEC §10's ``api`` trigger type exists for. The point of interest
is that it fires the *trigger* rather than calling the engine: L4-A shipped
``fire_api_trigger`` for this route and its docstring says so, and it is what
owns the contact lock, the supersede rule and the cross-workspace refusal.
Calling ``engine.start_flow`` directly would work, and would silently stop the
``api`` trigger type meaning anything.
"""

import json

import pytest

from apps.api.tests.conftest import bearer, make_key
from apps.contacts.models import Contact
from apps.flows.models import FlowExecution, StartedBy, Trigger, TriggerType
from apps.flows.tests.support import graph, node, published_flow

NOOP_ACTION = {"actions": [{"verb": "remove_tag", "tag": "not-a-tag-here"}]}


def api_flow(workspace, *, name="API flow", key=""):
    flow = published_flow(workspace, graph([node("a", "action", NOOP_ACTION)]), name=name)
    trigger = Trigger(flow=flow, type=TriggerType.API, config_json={"key": key} if key else {})
    trigger.save()
    return flow, trigger


def start(client, auth, contact, flow, **payload):
    return client.post(
        f"/api/v1/contacts/{contact.pk}/flows/{flow.pk}/start",
        data=json.dumps(payload),
        content_type="application/json",
        **auth,
    )


@pytest.fixture
def contact(db, tenancy):
    return Contact.objects.create(workspace=tenancy.workspace, first_name="Ada")


@pytest.mark.django_db
class TestStartingAFlow:
    def test_it_answers_202_with_the_execution_id(self, client, tenancy, auth, contact):
        flow, trigger = api_flow(tenancy.workspace)

        response = start(client, auth, contact, flow)

        assert response.status_code == 202
        body = response.json()
        execution = FlowExecution.objects.for_workspace(tenancy.workspace).get(pk=body["execution_id"])
        assert body["flow_id"] == str(flow.pk)
        assert body["contact_id"] == str(contact.pk)
        # It went through the trigger, not around it.
        assert execution.started_by == StartedBy.stamp(StartedBy.TRIGGER, trigger.pk)

    def test_a_trigger_key_selects_between_several(self, client, tenancy, auth, contact):
        flow, _ = api_flow(tenancy.workspace, key="onboard")
        wanted = Trigger(flow=flow, type=TriggerType.API, config_json={"key": "winback"})
        wanted.save()

        response = start(client, auth, contact, flow, trigger_key="winback")

        execution = FlowExecution.objects.for_workspace(tenancy.workspace).get(pk=response.json()["execution_id"])
        assert execution.started_by == StartedBy.stamp(StartedBy.TRIGGER, wanted.pk)

    def test_variables_reach_the_execution(self, client, tenancy, auth, contact):
        flow, _ = api_flow(tenancy.workspace)

        response = start(client, auth, contact, flow, variables={"order_id": "4711"})

        execution = FlowExecution.objects.for_workspace(tenancy.workspace).get(pk=response.json()["execution_id"])
        assert execution.variables["order_id"] == "4711"

    def test_a_flow_without_an_api_trigger_is_a_422(self, client, tenancy, auth, contact):
        flow = published_flow(tenancy.workspace, graph([node("a", "action", NOOP_ACTION)]), name="No trigger")

        response = start(client, auth, contact, flow)

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "no_api_trigger"

    def test_an_unpublished_flow_is_a_422(self, client, tenancy, auth, contact):
        from apps.flows.services import create_flow

        flow = create_flow(workspace=tenancy.workspace, name="Draft")
        Trigger(flow=flow, type=TriggerType.API).save()

        response = start(client, auth, contact, flow)

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "flow_not_runnable"

    def test_starting_again_supersedes_rather_than_stacking(self, client, tenancy, auth, contact):
        """SPEC §22: one live execution per contact; a new start supersedes."""
        flow, _ = api_flow(tenancy.workspace)

        start(client, auth, contact, flow)
        start(client, auth, contact, flow)

        live = FlowExecution.objects.for_workspace(tenancy.workspace).filter(
            contact=contact, status__in=["running", "waiting_reply", "waiting_delay"]
        )
        assert live.count() <= 1


@pytest.mark.django_db
class TestGuards:
    def test_an_oversized_variables_blob_is_refused(self, client, tenancy, auth, contact):
        """SECURITY-BASELINE §7. Flow variables are rendered into messages."""
        flow, _ = api_flow(tenancy.workspace)

        response = start(client, auth, contact, flow, variables={"blob": "x" * 20_000})

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_request"

    def test_a_deeply_nested_variables_blob_is_refused(self, client, tenancy, auth, contact):
        flow, _ = api_flow(tenancy.workspace)
        nested: dict = {"v": 1}
        for _ in range(10):
            nested = {"v": nested}

        response = start(client, auth, contact, flow, variables=nested)

        assert response.status_code == 422

    def test_another_workspaces_flow_is_a_404(self, client, tenancy, other_tenancy, auth, contact):
        theirs, _ = api_flow(other_tenancy.workspace, name="Theirs")

        assert start(client, auth, contact, theirs).status_code == 404

    def test_another_workspaces_contact_is_a_404(self, client, tenancy, other_tenancy, auth):
        flow, _ = api_flow(tenancy.workspace)
        stranger = Contact.objects.create(workspace=other_tenancy.workspace, first_name="Stranger")

        assert start(client, auth, stranger, flow).status_code == 404

    def test_a_read_key_cannot_start_a_flow(self, client, tenancy, contact):
        flow, _ = api_flow(tenancy.workspace)
        _, plaintext = make_key(tenancy.workspace, scopes=("read",))

        assert start(client, bearer(plaintext), contact, flow).status_code == 403
