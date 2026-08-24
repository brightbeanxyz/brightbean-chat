"""``sequence.subscribed`` / ``sequence.unsubscribed`` (ROADMAP contract 7).

Three consumers, three assertions: the catalog's shape, what a payload is allowed
to carry, and that an outbound webhook subscriber really receives one. The last
is the Layer-6 gate item — discovery is what makes it work, so it is asserted
through ``apps/api`` rather than by calling this app's emitter.
"""

import pytest
from django.dispatch import Signal

from apps.campaigns import services
from apps.campaigns.events import (
    EVENT_CATALOG,
    EVENT_SEQUENCE_SUBSCRIBED,
    EVENT_SEQUENCE_UNSUBSCRIBED,
    emit,
)
from apps.campaigns.tests.support import contact_for, sequence_with


class _Recorder:
    def __init__(self):
        self.payloads = []

    def __call__(self, sender=None, **payload):
        self.payloads.append(payload)


@pytest.fixture
def recorder():
    received = _Recorder()
    for signal in EVENT_CATALOG.values():
        signal.connect(received, weak=False)
    yield received
    for signal in EVENT_CATALOG.values():
        signal.disconnect(received)


class TestTheCatalog:
    def test_it_names_the_two_events_contract_seven_assigns_here(self):
        assert set(EVENT_CATALOG) == {"sequence.subscribed", "sequence.unsubscribed"}
        assert all(isinstance(signal, Signal) for signal in EVENT_CATALOG.values())

    def test_discovery_finds_it_without_a_registry(self):
        """``apps/api/events.py`` walks the installed apps; there is no list to
        add to, which is what makes L5-F pick this app up with no edit."""
        from apps.api.events import discover_catalog

        catalog = discover_catalog()

        assert catalog["sequence.subscribed"] is EVENT_CATALOG["sequence.subscribed"]
        assert catalog["sequence.unsubscribed"] is EVENT_CATALOG["sequence.unsubscribed"]

    def test_an_unknown_event_name_is_a_crash_not_a_silent_no_send(self):
        with pytest.raises(KeyError):
            emit("sequence.exploded", workspace_id="w", contact_id="c")


@pytest.mark.django_db
class TestThePayload:
    def test_subscribing_emits_ids_only(self, tenancy, recorder):
        sequence = sequence_with(tenancy.workspace, steps=1)
        contact = contact_for(tenancy.workspace, first_name="Ada")

        enrollment = services.subscribe(sequence, contact)

        (payload,) = recorder.payloads
        assert payload["event"] == EVENT_SEQUENCE_SUBSCRIBED
        assert payload["workspace_id"] == tenancy.workspace.pk
        assert payload["contact_id"] == contact.pk
        assert payload["sequence_id"] == sequence.pk
        assert payload["enrollment_id"] == enrollment.pk
        # No names, no step content, no message bodies — a webhook must not
        # become a PII egress path.
        assert "Ada" not in str(payload)
        assert "Onboarding" not in str(payload)

    def test_unsubscribing_emits_the_other_event(self, tenancy, recorder):
        sequence = sequence_with(tenancy.workspace, steps=2)
        contact = contact_for(tenancy.workspace)
        services.subscribe(sequence, contact)

        services.unsubscribe(sequence, contact)

        assert [payload["event"] for payload in recorder.payloads] == [
            EVENT_SEQUENCE_SUBSCRIBED,
            EVENT_SEQUENCE_UNSUBSCRIBED,
        ]

    def test_completing_a_sequence_is_not_an_unsubscribe(self, tenancy, recorder):
        """Somebody who reached the last step did not unsubscribe from anything."""
        sequence = sequence_with(tenancy.workspace, steps=0)

        services.subscribe(sequence, contact_for(tenancy.workspace))

        assert [payload["event"] for payload in recorder.payloads] == [EVENT_SEQUENCE_SUBSCRIBED]

    def test_re_enrollment_does_not_announce_an_unsubscribe(self, tenancy, recorder):
        """Otherwise every re-run of an onboarding flow fires every
        ``sequence_unsubscribed`` rule trigger in the workspace."""
        sequence = sequence_with(tenancy.workspace, steps=2)
        contact = contact_for(tenancy.workspace)
        services.subscribe(sequence, contact)
        recorder.payloads.clear()

        services.subscribe(sequence, contact)

        assert [payload["event"] for payload in recorder.payloads] == [EVENT_SEQUENCE_SUBSCRIBED]

    def test_unsubscribing_a_stranger_emits_nothing(self, tenancy, recorder):
        sequence = sequence_with(tenancy.workspace, steps=1)

        services.unsubscribe(sequence, contact_for(tenancy.workspace))

        assert recorder.payloads == []


@pytest.mark.django_db
class TestOutboundWebhookDelivery:
    """The Layer-6 gate item: both events reach an L5-F subscriber."""

    def _endpoint(self, workspace, events):
        from apps.api.models import OutboundWebhook

        endpoint = OutboundWebhook(workspace=workspace, url="https://example.test/hooks", events=list(events))
        endpoint.rotate_secret()
        endpoint.save()
        return endpoint

    def test_both_events_are_offered_to_subscribers(self):
        from apps.api.events import EVENT_LABELS, SUBSCRIBABLE_EVENTS

        assert {"sequence.subscribed", "sequence.unsubscribed"} <= set(SUBSCRIBABLE_EVENTS)
        assert all(EVENT_LABELS[name] for name in ("sequence.subscribed", "sequence.unsubscribed"))

    def test_a_subscribed_endpoint_gets_a_delivery_carrying_ids_only(self, tenancy):
        from apps.queueing.models import ScheduledAction

        endpoint = self._endpoint(tenancy.workspace, ["sequence.subscribed"])
        sequence = sequence_with(tenancy.workspace, steps=1)
        contact = contact_for(tenancy.workspace, first_name="Ada")

        enrollment = services.subscribe(sequence, contact)

        queued = ScheduledAction.objects.for_workspace(tenancy.workspace).filter(type="webhook_delivery")
        assert queued.count() == 1
        payload = queued.get().payload
        assert str(endpoint.pk) in str(payload)
        assert str(enrollment.pk) in str(payload)
        assert "Ada" not in str(payload)

    def test_an_endpoint_subscribed_to_something_else_gets_nothing(self, tenancy):
        from apps.queueing.models import ScheduledAction

        self._endpoint(tenancy.workspace, ["contact.created"])
        sequence = sequence_with(tenancy.workspace, steps=1)

        services.subscribe(sequence, contact_for(tenancy.workspace))

        assert not ScheduledAction.objects.for_workspace(tenancy.workspace).filter(type="webhook_delivery").exists()
