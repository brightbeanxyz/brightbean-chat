"""Subscribing to contract 7's event catalog.

The property that matters here is the one the issue asks for in as many words:
*"the subscription is data-driven so no code change is needed when it appears"*.
``broadcast.finished`` has no emitter until L6-B, so the test for it is about
discovery rather than delivery — a catalog assembled by walking the installed
apps picks up a new emitter the day it lands, and a hard-coded list does not.
"""

import pytest
from django.dispatch import Signal

from apps.api.delivery import ACTION_TYPE
from apps.api.events import SUBSCRIBABLE_EVENTS, connect_catalog_receivers, discover_catalog, on_catalog_event
from apps.api.models import OutboundWebhook
from apps.contacts.models import Contact, Tag
from apps.contacts.services import add_tag, create_contact
from apps.queueing.models import ScheduledAction


class TestDiscovery:
    def test_it_finds_every_apps_catalog(self):
        catalog = discover_catalog()

        assert {"contact.created", "contact.tag_added", "contact.tag_removed", "contact.field_changed"} <= set(catalog)
        assert "message.received" in catalog
        assert "execution.completed" in catalog
        assert all(isinstance(signal, Signal) for signal in catalog.values())

    def test_an_app_added_later_needs_no_change_here(self, monkeypatch):
        """The L6-B case, simulated.

        ``broadcast.finished`` is in the subscribable set today and has no
        emitter. Discovery is what makes it start working when one appears —
        this stubs an app whose ``events`` module carries the signal and asserts
        the union picks it up with nothing edited.
        """
        import importlib.machinery
        import sys
        import types

        from django.apps import apps as django_apps

        module = types.ModuleType("apps.fakecampaigns.events")
        # A real ModuleSpec, because a real module has one: importlib.find_spec
        # raises on an already-imported module without it, and a fake that
        # skipped it would be testing the wrong thing.
        module.__spec__ = importlib.machinery.ModuleSpec("apps.fakecampaigns.events", loader=None)
        module.EVENT_CATALOG = {"broadcast.finished": Signal()}
        monkeypatch.setitem(sys.modules, "apps.fakecampaigns.events", module)

        class FakeConfig:
            name = "apps.fakecampaigns"

        real = django_apps.get_app_configs
        monkeypatch.setattr(django_apps, "get_app_configs", lambda: [*real(), FakeConfig()])

        assert "broadcast.finished" in discover_catalog()

    def test_broadcast_finished_is_offered_even_though_nothing_emits_it(self):
        """SPEC §5 fixes the subscribable set; offering it is not a promise it fires."""
        assert "broadcast.finished" in SUBSCRIBABLE_EVENTS
        assert "broadcast.finished" not in discover_catalog()

    def test_connecting_twice_does_not_double_deliveries(self):
        """``dispatch_uid`` makes ``ready()`` idempotent.

        Worth pinning rather than assuming: the test suite reloads app configs,
        and without the uid a second ``ready()`` would silently double every
        webhook a workspace receives.
        """
        catalog = connect_catalog_receivers()
        signal = catalog["contact.created"]
        before = len(signal.receivers)

        connect_catalog_receivers()

        assert len(signal.receivers) == before


@pytest.mark.django_db
class TestFanOut:
    def test_a_subscribed_endpoint_gets_one_queued_delivery(self, tenancy, webhook):
        create_contact(tenancy.workspace, first_name="Ada", source="api")

        actions = ScheduledAction.objects.for_workspace(tenancy.workspace).filter(type=ACTION_TYPE)
        assert actions.count() == 1
        payload = actions.get().payload
        assert payload["event"] == "contact.created"
        assert payload["webhook_id"] == str(webhook.pk)
        assert payload["attempt"] == 1

    def test_an_unsubscribed_event_is_ignored(self, tenancy, webhook):
        webhook.events = ["message.received"]
        webhook.save(update_fields=["events"])

        create_contact(tenancy.workspace, first_name="Ada", source="api")

        assert not ScheduledAction.objects.for_workspace(tenancy.workspace).filter(type=ACTION_TYPE).exists()

    def test_a_disabled_endpoint_gets_nothing(self, tenancy, webhook):
        webhook.enabled = False
        webhook.save(update_fields=["enabled"])

        create_contact(tenancy.workspace, first_name="Ada", source="api")

        assert not ScheduledAction.objects.for_workspace(tenancy.workspace).filter(type=ACTION_TYPE).exists()

    def test_another_workspaces_endpoint_gets_nothing(self, tenancy, other_tenancy, webhook):
        create_contact(other_tenancy.workspace, first_name="Stranger", source="api")

        assert not ScheduledAction.objects.for_workspace(tenancy.workspace).filter(type=ACTION_TYPE).exists()

    def test_the_payload_carries_ids_only(self, tenancy, webhook):
        contact = Contact.objects.create(workspace=tenancy.workspace, first_name="Ada")
        tag = Tag.objects.create(workspace=tenancy.workspace, name="vip")
        add_tag(contact, tag)

        payload = ScheduledAction.objects.for_workspace(tenancy.workspace).filter(type=ACTION_TYPE).get().payload
        assert payload["data"] == {"contact_id": str(contact.pk), "tag_id": str(tag.pk)}

    def test_two_endpoints_get_one_delivery_each(self, tenancy, webhook):
        second = OutboundWebhook(
            workspace=tenancy.workspace,
            url="https://second.example.com/hooks",
            events=["contact.created"],
        )
        second.rotate_secret()
        second.save()

        create_contact(tenancy.workspace, first_name="Ada", source="api")

        actions = ScheduledAction.objects.for_workspace(tenancy.workspace).filter(type=ACTION_TYPE)
        assert {action.payload["webhook_id"] for action in actions} == {str(webhook.pk), str(second.pk)}

    def test_the_queued_row_names_no_contact(self, tenancy, webhook):
        """A contact-bearing queue row runs under that contact's advisory lock.

        Holding it across a ten-second call to someone else's server would stall
        every message for that contact behind a slow receiver (SPEC §9.6).
        """
        create_contact(tenancy.workspace, first_name="Ada", source="api")

        action = ScheduledAction.objects.for_workspace(tenancy.workspace).filter(type=ACTION_TYPE).get()
        assert action.contact_id is None

    def test_a_payload_missing_the_contract_fields_is_ignored(self, tenancy, webhook):
        on_catalog_event(None, event="", workspace_id=None)

        assert not ScheduledAction.objects.for_workspace(tenancy.workspace).filter(type=ACTION_TYPE).exists()
