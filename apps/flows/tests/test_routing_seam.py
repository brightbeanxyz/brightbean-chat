"""Contract 6's wiring: this app really does hold the routing slot."""

import pytest

from apps.channels import ingest as channels_ingest
from apps.flows.triggers.pipeline import ROUTING_PROCESSOR, route_events
from apps.messaging.ingest import PERSISTENCE_PROCESSOR, register_processors
from apps.messaging.ingest import ROUTING_PROCESSOR as MESSAGING_NAME
from apps.messaging.ingest import route_events as messaging_no_op


class TestTheSeam:
    def test_the_processor_name_matches_messagings_constant(self):
        """``apps.flows`` deliberately never imports ``apps.messaging`` at module
        scope — the property ``apps/flows/messaging.py`` exists to preserve — so
        the name is a local literal. This is what stops the two drifting."""
        assert ROUTING_PROCESSOR == MESSAGING_NAME

    def test_the_real_router_holds_the_slot_after_persistence(self):
        """Registering under an existing name replaces *in place*, so routing
        inherits the slot messaging's no-op was holding rather than appending
        after it — which is what makes "routing sees what persistence wrote"
        true without either app knowing about the other.

        Asserted as adjacency rather than as the whole tuple. The claim is about
        where routing sits *relative to persistence*, and the full-tuple form
        also silently asserted that no third stage exists anywhere — which stopped
        being true when issue #12 registered its preview stage at ``LATE_ORDER``,
        and would break again for every later stage that legitimately joins.
        """
        names = channels_ingest.registered_processors()

        assert names.index(ROUTING_PROCESSOR) == names.index(PERSISTENCE_PROCESSOR) + 1
        assert channels_ingest._PROCESSORS[ROUTING_PROCESSOR] is route_events

    def test_messagings_guard_never_puts_the_no_op_back(self):
        """``ready()`` runs in INSTALLED_APPS order and messaging is listed
        first, but nothing guarantees a later call. Its ``if ROUTING_PROCESSOR
        not in registered_processors()`` is what makes a second call harmless."""
        register_processors()

        assert channels_ingest._PROCESSORS[ROUTING_PROCESSOR] is route_events
        assert channels_ingest._PROCESSORS[ROUTING_PROCESSOR] is not messaging_no_op


@pytest.mark.django_db
class TestTheEndpointStillAcks:
    def test_a_router_that_raises_does_not_break_the_ack(self, tenancy, connection):
        """SPEC §7.1: never a 5xx for a business-logic failure. The seam turns a
        raising processor into a failed *batch*, and the platform still gets 200
        — because a 5xx makes it retry a delivery that will fail identically, and
        enough of those get a webhook disabled at the provider's end."""

        def explodes(connection, events):
            raise RuntimeError("boom")

        channels_ingest.register_processor(explodes, name=ROUTING_PROCESSOR)
        try:
            assert channels_ingest.process_events(connection, [object()]) is False
        finally:
            channels_ingest.register_processor(route_events, name=ROUTING_PROCESSOR)
