"""What ``MessagingConfig.ready()`` wires up, asserted after app loading.

The channels suite clears the contract-6 seam for the duration of each of its
tests, so this is the only place that checks the registrations actually happen
in a real process.
"""

import importlib.util

import pytest

from apps.channels import ingest as channels_ingest
from apps.contacts import conditions
from apps.messaging import ingest


class TestTheSeam:
    def test_both_stages_are_registered_in_dispatch_order(self) -> None:
        names = channels_ingest.registered_processors()
        assert ingest.PERSISTENCE_PROCESSOR in names
        assert names.index(ingest.PERSISTENCE_PROCESSOR) < names.index(ingest.ROUTING_PROCESSOR)

    def test_registering_twice_does_not_stack(self) -> None:
        """``ready()`` runs twice under some autoreload paths, and a processor
        registered twice would double-process every event."""
        before = channels_ingest.registered_processors()
        ingest.register_processors()
        assert channels_ingest.registered_processors() == before

    def test_replacing_the_routing_stage_keeps_its_position(self) -> None:
        """The mechanism contract 6 rests on: L4-A registers under our name and
        inherits the slot *after* persistence, with no edit to either app."""
        channels_ingest.register_processor(lambda c, e: None, name=ingest.ROUTING_PROCESSOR)
        names = channels_ingest.registered_processors()
        assert names.index(ingest.PERSISTENCE_PROCESSOR) < names.index(ingest.ROUTING_PROCESSOR)
        assert names.count(ingest.ROUTING_PROCESSOR) == 1

    def test_we_never_clobber_a_real_router_with_the_no_op(self) -> None:
        """``ready()`` runs in INSTALLED_APPS order. If L4-A's app sorts before
        this one, its router is already there — and replacing it with a no-op
        would stop routing with nothing raising anywhere."""
        marker: list[str] = []
        channels_ingest.register_processor(lambda c, e: marker.append("real"), name=ingest.ROUTING_PROCESSOR)
        ingest.register_processors()
        installed = channels_ingest._PROCESSORS[ingest.ROUTING_PROCESSOR]
        assert installed is not ingest.route_events


class TestTheConditionSource:
    def test_window_is_evaluable_after_app_loading(self) -> None:
        assert conditions.sources()["window"].is_evaluable


class TestNoEndpoints:
    def test_this_app_adds_no_routes(self) -> None:
        """PR 1 adds no HTTP surface — the inbox is #14 and the API is #25 — so
        the IDOR suite has nothing to sweep. Asserting it means the obligation
        is met on purpose rather than forgotten, and the first route added here
        turns this red and sends its author to ``tests/idor.py``.
        """
        assert importlib.util.find_spec("apps.messaging.urls") is None

    def test_the_idor_sweep_sees_no_messaging_route(self) -> None:
        from tests.idor import iter_tenant_routes

        assert not [route for route in iter_tenant_routes() if route.name.startswith("messaging")]


@pytest.mark.django_db
class TestTheAdminIsReadOnly:
    """Message bodies are the largest concentration of attacker-controlled text
    in the product. The admin is a superuser's window onto it, never an editor."""

    def test_no_messaging_model_can_be_changed_through_the_admin(self) -> None:
        from django.contrib import admin

        from apps.messaging.models import ContactChannelIdentity, Conversation, Message

        for model in (ContactChannelIdentity, Conversation, Message):
            options = admin.site._registry[model]
            assert options.has_add_permission(None) is False  # type: ignore[arg-type]
            assert options.has_change_permission(None) is False  # type: ignore[arg-type]
            assert options.has_delete_permission(None) is False  # type: ignore[arg-type]

    def test_no_message_body_is_rendered_in_a_list_column(self) -> None:
        from django.contrib import admin

        from apps.messaging.models import ContactChannelIdentity, Message

        assert "body" not in admin.site._registry[Message].list_display
        assert "extra" not in admin.site._registry[ContactChannelIdentity].list_display
