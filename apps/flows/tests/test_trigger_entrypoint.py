"""SPEC §10's `api` trigger — the door #25 will knock on."""

import threading

import pytest
from django.db import connections, transaction
from django.utils import timezone

from apps.flows.models import FlowExecution, StartedBy, Trigger, TriggerType
from apps.flows.tests.support import connection_for, contact_for, graph, node, published_flow
from apps.flows.triggers.entrypoints import fire_api_trigger
from apps.flows.triggers.matching import registered_matchers
from apps.queueing.models import ActionType, ScheduledAction

NOOP_ACTION = {"actions": [{"verb": "remove_tag", "tag": "not-a-tag-here"}]}


def _flow(workspace, name="API flow"):
    return published_flow(workspace, graph([node("a", "action", NOOP_ACTION)]), name=name)


def _api_trigger(flow, *, key=""):
    trigger = Trigger(flow=flow, type=TriggerType.API, config_json={"key": key} if key else {})
    trigger.save()
    return trigger


@pytest.mark.django_db
class TestFireApiTrigger:
    def test_it_has_no_matcher_and_never_fires_from_a_webhook(self):
        assert TriggerType.API not in registered_matchers()

    def test_it_starts_the_flow(self, tenancy, contact):
        flow = _flow(tenancy.workspace)
        trigger = _api_trigger(flow)

        result = fire_api_trigger(flow=flow, contact=contact)

        assert result.started is True
        assert result.execution.started_by == StartedBy.stamp(StartedBy.TRIGGER, trigger.pk)

    def test_a_flow_with_no_api_trigger_is_refused(self, tenancy, contact):
        flow = _flow(tenancy.workspace)

        result = fire_api_trigger(flow=flow, contact=contact)

        assert result.started is False
        assert result.reason == "no_api_trigger"

    def test_a_key_selects_between_several(self, tenancy, contact):
        flow = _flow(tenancy.workspace)
        _api_trigger(flow, key="onboard")
        wanted = _api_trigger(flow, key="winback")

        result = fire_api_trigger(flow=flow, contact=contact, key="winback")

        assert result.trigger.pk == wanted.pk

    def test_a_contact_from_another_workspace_is_refused(self, tenancy, other_tenancy):
        flow = _flow(tenancy.workspace)
        _api_trigger(flow)
        stranger = contact_for(other_tenancy.workspace)

        result = fire_api_trigger(flow=flow, contact=stranger)

        assert result.started is False
        assert result.reason == "cross_workspace"
        assert not FlowExecution.objects.for_workspace(tenancy.workspace).exists()

    def test_it_resolves_a_connection_from_the_contacts_identity(self, tenancy, contact):
        """The open question apps/flows/engine/sending.py leaves for this layer."""
        from apps.messaging.models import ContactChannelIdentity

        connection = connection_for(tenancy.workspace, external_id="bot-1")
        ContactChannelIdentity(
            contact=contact,
            channel_connection=connection,
            platform_user_id="tg-1",
            opt_in=True,
            opt_in_at=timezone.now(),
            opt_in_source="message_in",
            last_inbound_at=timezone.now(),
        ).save()
        flow = _flow(tenancy.workspace)
        _api_trigger(flow)

        result = fire_api_trigger(flow=flow, contact=contact)

        assert result.execution.channel_connection_id == connection.pk

    def test_variables_reach_the_execution(self, tenancy, contact):
        flow = _flow(tenancy.workspace)
        _api_trigger(flow)

        result = fire_api_trigger(flow=flow, contact=contact, variables={"plan": "pro"})

        assert result.execution.variables["plan"] == "pro"


@pytest.mark.django_db(transaction=True)
class TestUnderContention:
    def test_it_enqueues_rather_than_blocking(self, tenancy):
        """It is a request too — a caller waiting behind the worker is a held
        connection rather than a slow one."""
        from apps.queueing.locks import contact_lock

        contact = contact_for(tenancy.workspace)
        flow = _flow(tenancy.workspace)
        _api_trigger(flow)

        held = threading.Event()
        release = threading.Event()

        def holder():
            try:
                with transaction.atomic(), contact_lock(contact):
                    held.set()
                    release.wait(timeout=15)
            finally:
                connections.close_all()

        thread = threading.Thread(target=holder)
        thread.start()
        try:
            assert held.wait(timeout=10)
            result = fire_api_trigger(flow=flow, contact=contact)

            assert result.execution is None
            assert result.scheduled is not None
            assert result.scheduled.type == ActionType.START_FLOW
        finally:
            release.set()
            thread.join(timeout=15)
            assert not thread.is_alive()
            ScheduledAction.objects.unscoped().filter(workspace=tenancy.workspace).delete()
            FlowExecution.objects.unscoped().filter(contact=contact).delete()
