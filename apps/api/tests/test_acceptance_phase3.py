"""SPEC §21 phase 3, as one test.

    Accept when: a Make/Zapier-style scenario (inbound webhook → API contact
    update → flow start) works with only public API + outbound webhooks.

That is the litmus test the whole issue exists for, so it is written as one
scenario rather than as five unit tests that each mock the next stage. Nothing
here reaches inside: every step is an HTTP call an integrator could make with
``curl``, and the final assertion is on the bytes a third-party receiver would
have to verify.

The one thing standing in for real infrastructure is the network, replaced at
``httpx.HTTPTransport.handle_request`` — the SSRF guard, the queue handler, the
signature and the facades are all the production code paths.
"""

import hashlib
import hmac
import json
import time

import pytest

from apps.api.delivery import ACTION_TYPE, SIGNATURE_HEADER, TIMESTAMP_HEADER, handle_webhook_delivery
from apps.api.models import OutboundWebhook, WebhookDelivery
from apps.api.tests.conftest import bearer, make_key
from apps.api.tests.support import PUBLIC, RECEIVER, FakeInternet, serving
from apps.common.outbound import reset_deployment_cache
from apps.contacts.models import Contact
from apps.flows.models import FlowExecution, Trigger, TriggerType
from apps.flows.tests.support import graph, node, published_flow
from apps.queueing.models import ActionStatus, ScheduledAction
from tests.ssrf import guard_required

NOOP_ACTION = {"actions": [{"verb": "remove_tag", "tag": "not-a-tag-here"}]}


@pytest.fixture(autouse=True)
def _clear_deployment_cache():
    reset_deployment_cache()
    yield
    reset_deployment_cache()


def receiver_verifies(secret: str, request) -> bool:
    """What a third-party receiver would run, written from ``docs/api/v1.md``."""
    timestamp = request.headers[TIMESTAMP_HEADER]
    presented = request.headers[SIGNATURE_HEADER]
    if abs(time.time() - int(timestamp)) > 300:
        return False
    signed = timestamp.encode() + b"." + request.content
    expected = "v1=" + hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, presented)


def drain(workspace):
    """Run the queued webhook deliveries the way the worker would."""
    for action in list(
        ScheduledAction.objects.for_workspace(workspace).filter(type=ACTION_TYPE, status=ActionStatus.PENDING)
    ):
        action.status = ActionStatus.DONE
        action.save(update_fields=["status"])
        handle_webhook_delivery(action.payload, action)


@pytest.mark.django_db
class TestPhaseThreeScenario:
    def test_inbound_webhook_to_contact_update_to_flow_start_to_signed_delivery(self, client, tenancy, monkeypatch):
        internet = FakeInternet(serving(200)).install(monkeypatch)

        # --- What the operator sets up, once, in the UI -------------------
        endpoint = OutboundWebhook(
            workspace=tenancy.workspace,
            url=f"https://{RECEIVER}/hooks",
            events=["contact.created", "contact.tag_added", "execution.completed"],
        )
        secret = endpoint.rotate_secret()
        endpoint.save()

        flow = published_flow(tenancy.workspace, graph([node("a", "action", NOOP_ACTION)]), name="Welcome")
        Trigger(flow=flow, type=TriggerType.API, config_json={"key": "signup"}).save()

        _, token = make_key(tenancy.workspace, scopes=("read", "write"), name="Zapier")
        auth = bearer(token)

        # --- Step 1: the scenario creates the contact --------------------
        created = client.post(
            "/api/v1/contacts",
            data=json.dumps({"first_name": "Ada", "email": "ada@example.com"}),
            content_type="application/json",
            **auth,
        )
        assert created.status_code == 201
        contact_id = created.json()["id"]

        # --- Step 2: it updates them and tags them -----------------------
        patched = client.patch(
            f"/api/v1/contacts/{contact_id}",
            data=json.dumps({"phone": "+15550001"}),
            content_type="application/json",
            **auth,
        )
        assert patched.status_code == 200

        tagged = client.post(
            f"/api/v1/contacts/{contact_id}/tags",
            data=json.dumps({"name": "signed-up"}),
            content_type="application/json",
            **auth,
        )
        assert tagged.status_code == 201

        # --- Step 3: it starts the flow ----------------------------------
        started = client.post(
            f"/api/v1/contacts/{contact_id}/flows/{flow.pk}/start",
            data=json.dumps({"trigger_key": "signup", "variables": {"plan": "pro"}}),
            content_type="application/json",
            **auth,
        )
        assert started.status_code == 202
        execution_id = started.json()["execution_id"]

        contact = Contact.objects.for_workspace(tenancy.workspace).get(pk=contact_id)
        assert contact.phone == "+15550001"
        assert list(contact.tags.values_list("name", flat=True)) == ["signed-up"]
        execution = FlowExecution.objects.for_workspace(tenancy.workspace).get(pk=execution_id)
        assert execution.variables["plan"] == "pro"

        # --- Step 4: the scenario's receiver hears about all of it -------
        with guard_required() as guarded:
            drain(tenancy.workspace)

        assert len(guarded) == 3, "one delivery per subscribed event, all through the guard"
        assert {request.url.host for request in internet.requests} == {PUBLIC}, "pinned to the checked address"

        delivered = {}
        for request in internet.requests:
            assert receiver_verifies(secret, request), "a receiver can verify every delivery"
            document = json.loads(request.content)
            delivered[document["event"]] = document

        assert set(delivered) == {"contact.created", "contact.tag_added", "execution.completed"}

        completion = delivered["execution.completed"]
        assert completion["workspace_id"] == str(tenancy.workspace.pk)
        assert completion["data"]["execution_id"] == str(execution.pk)
        assert completion["data"]["flow_id"] == str(flow.pk)
        assert completion["data"]["contact_id"] == str(contact.pk)

        assert WebhookDelivery.objects.for_workspace(tenancy.workspace).filter(status="succeeded").count() == 3

    def test_a_tampered_delivery_is_rejected_by_the_sample_verifier(self, tenancy, monkeypatch, webhook):
        """The other half of the round trip.

        A verifier that accepts everything would make the assertion above
        vacuous, so the same function has to reject a body that was changed in
        flight.
        """
        internet = FakeInternet(serving(200)).install(monkeypatch)
        from apps.api.delivery import send_test_event

        send_test_event(webhook)
        sent = internet.requests[0]

        assert receiver_verifies(webhook.secret, sent)

        tampered = sent.__class__(sent.method, sent.url, headers=sent.headers, content=b'{"event":"contact.created"}')
        assert not receiver_verifies(webhook.secret, tampered)
