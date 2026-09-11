"""The graph a new flow opens with.

It reaches the author before they have touched anything, so it has to be clean
on arrival — not merely storable. A seed that validates with a warning would
make the first thing the builder ever says to somebody a complaint about a node
they did not add.
"""

import pytest

from apps.common.platforms import Platform
from apps.flows.schema.envelope import empty_graph
from apps.flows.schema.validation import validate_graph
from apps.flows.services import create_flow
from apps.flows.starter import starter_graph


class TestTheStarterGraph:
    def test_it_is_valid_and_publishable(self):
        result = validate_graph(starter_graph())

        assert result.errors == []
        assert result.is_publishable

    @pytest.mark.parametrize("platform", list(Platform.values))
    def test_it_raises_nothing_on_any_platform(self, platform):
        """The one that catches "somebody changed the seed to a card, and now
        every WhatsApp workspace opens a brand-new flow to a downgrade warning
        about a node they have not touched"."""
        result = validate_graph(starter_graph(), platforms=(platform,))

        assert result.errors == []
        assert result.warnings == []

    def test_it_has_exactly_one_node_and_no_edges(self):
        graph = starter_graph()

        assert len(graph["nodes"]) == 1
        assert graph["nodes"][0]["type"] == "send_message"
        assert graph["edges"] == []

    def test_two_calls_do_not_share_mutable_state(self):
        """The position dict is copied per call; without that, dragging the seed
        node in one flow would move it in the next one created this process."""
        first = starter_graph()
        first["nodes"][0]["position"]["x"] = 9999

        assert starter_graph()["nodes"][0]["position"]["x"] != 9999


@pytest.mark.django_db
class TestWhoGetsIt:
    def test_create_flow_still_starts_empty_unless_a_graph_is_asked_for(self, tenancy):
        """The default is what keeps "still empty" meaning "nothing was written"
        in the importer's and the composer's tests."""
        flow = create_flow(workspace=tenancy.workspace, name="Plain")

        assert flow.versions.get(version=1).graph_json == empty_graph()

    def test_a_graph_that_is_asked_for_is_the_one_stored(self, tenancy):
        flow = create_flow(workspace=tenancy.workspace, name="Seeded", graph=starter_graph())

        assert flow.versions.get(version=1).graph_json == starter_graph()
