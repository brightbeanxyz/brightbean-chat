"""The validation matrix — one case per error class, plus the tier boundary.

The tier boundary is the part worth reading first: a *document* finding refuses
the write (it is the mass-assignment or input-limit guard), while a *graph*
finding saves happily and blocks only publish. Get that backwards and either the
builder's autosave deletes work, or an unknown config key reaches the database.
"""

from copy import deepcopy
from typing import Any

from apps.flows.fixtures import graph_for, node_fixture
from apps.flows.schema import MAX_EDGES, MAX_GRAPH_BYTES, MAX_GRAPH_DEPTH, MAX_NODES, empty_graph, validate_graph


def codes(graph: Any, **kwargs: Any) -> list[str]:
    return [issue.code for issue in validate_graph(graph, **kwargs).errors]


def warning_codes(graph: Any, **kwargs: Any) -> list[str]:
    return [issue.code for issue in validate_graph(graph, **kwargs).warnings]


def _single_node_graph(node_type: str = "note") -> dict[str, Any]:
    graph = empty_graph()
    graph["nodes"] = [node_fixture(node_type, node_id="n1")]
    return graph


class TestTheTierBoundary:
    def test_a_structural_finding_refuses_the_save(self):
        graph = _single_node_graph()
        graph["nodes"][0]["config"]["surprise"] = "value"

        result = validate_graph(graph)

        assert result.blocks_save is True
        assert [issue.code for issue in result.document_errors] == ["unknown_config_key"]

    def test_a_half_wired_graph_saves_and_only_blocks_publish(self):
        graph = graph_for("send_message")
        graph["edges"].append({"id": "x", "source": "subject", "sourceHandle": "default", "target": "gone"})

        result = validate_graph(graph)

        assert result.blocks_save is False
        assert result.is_publishable is False
        assert [issue.code for issue in result.graph_errors] == ["dangling_edge"]

    def test_a_valid_graph_is_clean(self):
        result = validate_graph(graph_for("send_message"))

        assert result.errors == []
        assert result.warnings == []
        assert result.is_publishable is True


class TestInputLimits:
    """SECURITY-BASELINE §7: size and depth caps before anything walks the graph."""

    def test_an_oversized_graph_is_refused(self):
        graph = empty_graph()
        graph["nodes"] = [node_fixture("note", node_id="n1")]
        graph["nodes"][0]["config"]["text"] = "x" * (MAX_GRAPH_BYTES + 1)

        assert codes(graph) == ["graph_too_large"]

    def test_a_deeply_nested_graph_is_refused(self):
        graph = empty_graph()
        node = node_fixture("external_request", node_id="n1")
        nested: Any = "leaf"
        for _ in range(MAX_GRAPH_DEPTH + 5):
            nested = {"deeper": nested}
        node["config"]["body"] = nested
        graph["nodes"] = [node]

        assert codes(graph) == ["graph_too_deep"]

    def test_the_depth_walk_gives_up_early(self):
        """The measurement itself must not be the denial of service: it stops as
        soon as the answer is "too deep", rather than measuring how deep."""
        from apps.flows.schema.envelope import json_depth

        nested: Any = "leaf"
        for _ in range(5000):
            nested = [nested]

        assert json_depth(nested, limit=10) == 11

    def test_too_many_nodes_is_refused(self):
        graph = empty_graph()
        graph["nodes"] = [
            {"id": f"n{index}", "type": "note", "position": {"x": 0, "y": 0}, "config": {"text": "."}}
            for index in range(MAX_NODES + 1)
        ]

        assert codes(graph) == ["too_many_nodes"]

    def test_too_many_edges_is_refused(self):
        graph = empty_graph()
        graph["edges"] = [
            {"id": f"e{index}", "source": "a", "sourceHandle": "default", "target": "b"}
            for index in range(MAX_EDGES + 1)
        ]

        assert codes(graph) == ["too_many_edges"]

    def test_a_value_that_is_not_json_at_all_is_refused(self):
        assert codes({"schema": 1, "nodes": {"not", "json"}, "edges": []}) == ["malformed_graph"]

    def test_non_finite_numbers_are_refused(self):
        """CPython's decoder accepts bare NaN/Infinity and overflows 1e999 to
        inf. Postgres jsonb refuses all three, so storing one turns an
        authenticated save into a 500."""
        graph = _single_node_graph()
        graph["nodes"][0]["position"]["x"] = float("inf")

        assert codes(graph) == ["malformed_graph"]

    def test_non_finite_numbers_are_refused_on_the_fast_path_too(self):
        """The known_size short-circuit skips serialising, which is where the
        first check lives — so the walk has to catch it as well."""
        graph = _single_node_graph()
        graph["nodes"][0]["position"]["y"] = float("nan")

        assert codes(graph, known_size=64) == ["non_finite_number"]


class TestEnvelope:
    def test_a_graph_must_be_an_object(self):
        assert codes([]) == ["graph_not_object"]

    def test_an_unknown_top_level_key_is_refused(self):
        graph = empty_graph()
        graph["viewport"] = {"zoom": 1}

        assert codes(graph) == ["unknown_top_level_key"]

    def test_a_foreign_schema_version_is_refused(self):
        graph = empty_graph()
        graph["schema"] = 2

        assert codes(graph) == ["unsupported_schema_version"]

    def test_nodes_and_edges_must_be_lists(self):
        assert codes({"schema": 1, "nodes": {}, "edges": []}) == ["nodes_not_list"]
        assert codes({"schema": 1, "nodes": [], "edges": {}}) == ["edges_not_list"]


class TestNodes:
    def test_a_node_must_be_an_object(self):
        graph = empty_graph()
        graph["nodes"] = ["n1"]

        assert codes(graph) == ["node_not_object"]

    def test_a_node_id_is_an_allowlist(self):
        graph = _single_node_graph()
        graph["nodes"][0]["id"] = "n1; DROP TABLE"

        assert codes(graph) == ["invalid_node_id"]

    def test_duplicate_node_ids_are_refused(self):
        graph = empty_graph()
        graph["nodes"] = [node_fixture("note", node_id="n1"), node_fixture("note", node_id="n1")]

        assert "duplicate_node_id" in codes(graph)

    def test_an_unknown_node_key_is_refused(self):
        """React Flow decorates its nodes with view state. The builder
        serialises it away; anything that does not is refused here."""
        graph = _single_node_graph()
        graph["nodes"][0]["selected"] = True

        assert codes(graph) == ["unknown_node_key"]

    def test_a_position_must_be_two_numbers(self):
        graph = _single_node_graph()
        graph["nodes"][0]["position"] = {"x": "0", "y": 0}

        assert codes(graph) == ["invalid_position"]

    def test_an_unknown_node_type_is_refused(self):
        graph = _single_node_graph()
        graph["nodes"][0]["type"] = "send_carrier_pigeon"

        assert codes(graph) == ["unknown_node_type"]

    def test_a_missing_config_is_refused(self):
        graph = _single_node_graph()
        del graph["nodes"][0]["config"]

        assert codes(graph) == ["missing_required_config"]


class TestConfigs:
    def test_an_unknown_config_key_is_refused(self):
        graph = _single_node_graph("send_sms")
        graph["nodes"][0]["config"]["from_number"] = "+15550000"

        assert codes(graph) == ["unknown_config_key"]

    def test_unknown_keys_are_refused_at_depth_too(self):
        """The mass-assignment guard is not a top-level courtesy: every object
        the schema builds is closed, however deeply nested."""
        graph = _single_node_graph("send_message")
        graph["nodes"][0]["config"]["buttons"][0]["payload"] = {"admin": True}

        assert codes(graph) == ["unknown_config_key"]

    def test_a_missing_required_key_is_refused(self):
        graph = _single_node_graph("send_email")
        del graph["nodes"][0]["config"]["subject"]

        assert codes(graph) == ["missing_required_config"]

    def test_a_wrong_type_is_refused(self):
        graph = _single_node_graph("send_sms")
        graph["nodes"][0]["config"]["text"] = 42

        assert codes(graph) == ["invalid_config_value"]

    def test_a_boolean_is_not_an_integer(self):
        """isinstance(True, int) is True in Python. Without the explicit
        exclusion, timeout_s: true would validate and reach the engine."""
        graph = _single_node_graph("external_request")
        graph["nodes"][0]["config"]["timeout_s"] = True

        assert codes(graph) == ["invalid_config_value"]

    def test_a_value_out_of_range_is_refused(self):
        graph = _single_node_graph("external_request")
        graph["nodes"][0]["config"]["timeout_s"] = 30

        assert codes(graph) == ["invalid_config_value"]

    def test_an_unknown_enum_member_is_refused(self):
        graph = _single_node_graph("data_collection")
        graph["nodes"][0]["config"]["reply_type"] = "iban"

        assert codes(graph) == ["invalid_config_value"]

    def test_an_unknown_block_type_is_reported_against_the_discriminator(self):
        graph = _single_node_graph("send_message")
        graph["nodes"][0]["config"]["blocks"][0] = {"type": "hologram"}

        result = validate_graph(graph)

        assert [issue.code for issue in result.errors] == ["invalid_config_value"]
        assert result.errors[0].path.endswith(".type")

    def test_an_unknown_action_verb_is_refused(self):
        graph = _single_node_graph("action")
        graph["nodes"][0]["config"]["actions"] = [{"verb": "grant_admin"}]

        assert codes(graph) == ["invalid_config_value"]

    def test_a_condition_operator_outside_the_table_is_refused(self):
        graph = _single_node_graph("condition")
        graph["nodes"][0]["config"]["rules"][0]["op"] = "regex"

        assert codes(graph) == ["invalid_config_value"]

    def test_a_smart_delay_must_carry_the_payload_its_mode_names(self):
        """Only `mode` used to be required, so {"mode": "duration"} published
        cleanly and reached the engine with nothing to compute run_at from."""
        graph = _single_node_graph("smart_delay")
        graph["nodes"][0]["config"] = {"mode": "duration"}

        assert codes(graph) == ["missing_required_config"]

    def test_a_smart_delay_rejects_the_other_mode_payload(self):
        graph = _single_node_graph("smart_delay")
        graph["nodes"][0]["config"] = {"mode": "date", "duration": {"value": 5, "unit": "minutes"}}

        assert "unknown_config_key" in codes(graph)

    def test_a_date_smart_delay_needs_a_field_or_a_datetime(self):
        graph = _single_node_graph("smart_delay")
        graph["nodes"][0]["config"] = {"mode": "date", "date": {}}

        assert codes(graph) == ["missing_required_config"]

    def test_both_smart_delay_modes_validate_when_complete(self):
        for config in (
            {"mode": "duration", "duration": {"value": 5, "unit": "minutes"}},
            {"mode": "date", "date": {"field": "birthday"}},
            {"mode": "date", "date": {"datetime": "2026-01-01T00:00:00Z"}},
        ):
            graph = _single_node_graph("smart_delay")
            graph["nodes"][0]["config"] = config
            assert codes(graph) == [], config

    def test_a_media_block_needs_an_id_or_a_url(self):
        graph = _single_node_graph("send_message")
        graph["nodes"][0]["config"]["blocks"][1] = {"type": "image", "caption": "no source"}

        assert codes(graph) == ["missing_required_config"]


class TestEdges:
    def test_an_edge_must_be_an_object(self):
        graph = graph_for("send_message")
        graph["edges"][0] = "e1"

        assert codes(graph) == ["edge_not_object"]

    def test_duplicate_edge_ids_are_refused(self):
        graph = graph_for("send_message")
        graph["edges"][1]["id"] = graph["edges"][0]["id"]

        assert "duplicate_edge_id" in codes(graph)

    def test_an_unknown_edge_key_is_refused(self):
        graph = graph_for("send_message")
        graph["edges"][0]["targetHandle"] = None

        assert codes(graph) == ["unknown_edge_key"]

    def test_an_endpoint_must_be_a_non_empty_string(self):
        graph = graph_for("send_message")
        graph["edges"][0]["target"] = ""

        assert codes(graph) == ["invalid_edge_endpoint"]

    def test_a_dangling_edge_is_a_graph_error(self):
        graph = graph_for("send_message")
        graph["edges"][0]["target"] = "missing"

        assert codes(graph) == ["dangling_edge"]

    def test_a_malformed_handle_is_refused(self):
        graph = graph_for("send_message")
        graph["edges"][0]["sourceHandle"] = "btn:has spaces"

        assert codes(graph) == ["malformed_handle"]

    def test_a_handle_the_node_does_not_expose_is_refused(self):
        graph = graph_for("send_message")
        graph["edges"][0]["sourceHandle"] = "btn:not-a-button"

        assert codes(graph) == ["handle_not_available"]

    def test_a_handle_stops_being_valid_when_its_button_goes_away(self):
        """Handles are derived from the config, so deleting a button in the
        panel invalidates its edge rather than leaving a dead route behind."""
        graph = graph_for("send_message")
        graph["nodes"][0]["config"]["buttons"] = [graph["nodes"][0]["config"]["buttons"][1]]

        assert codes(graph) == ["handle_not_available"]

    def test_two_edges_on_one_handle_are_refused(self):
        graph = graph_for("send_message")
        duplicate = deepcopy(graph["edges"][0])
        duplicate["id"] = "second"
        graph["edges"].append(duplicate)

        assert codes(graph) == ["duplicate_handle_edge"]

    def test_a_terminal_node_may_not_route_onward(self):
        graph = graph_for("start_flow")
        graph["nodes"].append(node_fixture("note", node_id="tail"))
        graph["edges"] = [{"id": "e1", "source": "subject", "sourceHandle": "default", "target": "tail"}]

        assert "terminal_node_has_outgoing_edge" in codes(graph)

    def test_a_note_may_not_be_connected(self):
        graph = graph_for("send_message")
        graph["nodes"].append(node_fixture("note", node_id="memo"))
        graph["edges"][0]["target"] = "memo"

        assert codes(graph) == ["note_node_connected"]


class TestEntryNodes:
    def test_an_empty_graph_has_no_entry(self):
        assert codes(empty_graph()) == ["no_entry_node"]

    def test_a_graph_of_notes_alone_has_no_entry(self):
        graph = empty_graph()
        graph["nodes"] = [node_fixture("note", node_id="n1")]

        assert codes(graph) == ["no_entry_node"]

    def test_a_closed_cycle_has_no_entry(self):
        graph = graph_for("send_message")
        graph["edges"].append({"id": "back", "source": "sink", "sourceHandle": "default", "target": "subject"})

        assert codes(graph) == ["no_entry_node"]

    def test_two_unconnected_starts_are_refused_and_both_are_named(self):
        graph = graph_for("send_message")
        graph["nodes"].append(node_fixture("send_sms", node_id="second"))

        result = validate_graph(graph)

        assert {issue.code for issue in result.errors} == {"multiple_entry_nodes"}
        assert {issue.node_id for issue in result.errors} == {"subject", "second"}

    def test_a_node_that_loops_to_itself_is_still_the_entry(self):
        """SPEC §9.1 says "no incoming edges **from other nodes**". Counting a
        self-edge made the commonest retry shape there is — a question whose
        `timeout` handle re-asks itself — report "nowhere to start"."""
        graph = empty_graph()
        node = node_fixture("send_message", node_id="ask")
        graph["nodes"] = [node]
        graph["edges"] = [{"id": "retry", "source": "ask", "sourceHandle": "timeout", "target": "ask"}]

        assert validate_graph(graph).errors == []

    def test_an_edge_from_a_note_does_not_steal_entry_status(self):
        """A note takes no part in routing, so an edge out of one must not make
        its target look like it has an incoming edge — that reported a missing
        entry node on top of the note error describing the actual mistake."""
        graph = empty_graph()
        graph["nodes"] = [node_fixture("send_message", node_id="ask"), node_fixture("note", node_id="memo")]
        graph["edges"] = [{"id": "e1", "source": "memo", "sourceHandle": "default", "target": "ask"}]

        assert "no_entry_node" not in codes(graph)

    def test_a_cycle_back_to_the_entry_says_which_loop_to_break(self):
        graph = graph_for("send_message")
        graph["edges"].append({"id": "back", "source": "sink", "sourceHandle": "default", "target": "subject"})

        result = validate_graph(graph)

        assert [issue.code for issue in result.errors] == ["no_entry_node"]
        assert "break the loop" in result.errors[0].message

    def test_a_cycle_downstream_of_the_entry_is_allowed(self):
        """SPEC §9.1 allows cycles outright — the runtime's blocks_since_pause
        cap is what protects against a runaway loop, not the validator."""
        graph = graph_for("send_message")
        graph["nodes"].append(node_fixture("send_sms", node_id="second"))
        graph["edges"].append({"id": "on", "source": "sink", "sourceHandle": "default", "target": "second"})
        graph["edges"].append({"id": "back", "source": "second", "sourceHandle": "default", "target": "sink"})

        assert validate_graph(graph).errors == []


class TestWarnings:
    def test_an_unreachable_node_is_a_warning_not_an_error(self):
        graph = graph_for("send_message")
        graph["nodes"].append(node_fixture("send_sms", node_id="island"))
        graph["nodes"].append(node_fixture("action", node_id="island_tail"))
        graph["edges"].append({"id": "iso", "source": "island", "sourceHandle": "default", "target": "island_tail"})
        # `island` now has no incoming edge either, which would be a second
        # entry; give it one so the only finding left is reachability.
        graph["edges"].append({"id": "iso2", "source": "island_tail", "sourceHandle": "default", "target": "island"})

        result = validate_graph(graph)

        assert result.errors == []
        assert set(warning_codes(graph)) == {"unreachable_node"}
        assert {issue.node_id for issue in result.warnings} == {"island", "island_tail"}


class TestDuplicateReplyIds:
    """A button and a quick reply on one node may not share an id.

    Both are legal handles, but an inbound reply carries only the id — so the
    two cannot be told apart and the node cannot be routed.
    """

    def _node(self, buttons, quick_replies):
        return {
            "id": "ask",
            "type": "send_message",
            "position": {"x": 0, "y": 0},
            "config": {
                "blocks": [{"type": "text", "text": "Pick:"}],
                "buttons": buttons,
                "quick_replies": quick_replies,
            },
        }

    def _validate(self, buttons, quick_replies):
        graph = empty_graph()
        graph["nodes"] = [self._node(buttons, quick_replies)]
        return validate_graph(graph)

    def test_a_shared_id_is_a_graph_error(self):
        result = self._validate(
            [{"id": "yes", "label": "Yes", "action": "postback"}],
            [{"id": "yes", "label": "Yes please"}],
        )

        assert not result.is_publishable
        assert [issue.code for issue in result.errors] == ["duplicate_reply_id"]

    def test_distinct_ids_are_fine(self):
        result = self._validate(
            [{"id": "yes", "label": "Yes", "action": "postback"}],
            [{"id": "later", "label": "Later"}],
        )

        assert result.is_publishable

    def test_it_is_a_graph_error_so_the_draft_still_saves(self):
        """SPEC §16 autosaves every two seconds; a half-wired draft must persist."""
        result = self._validate(
            [{"id": "yes", "label": "Yes", "action": "postback"}],
            [{"id": "yes", "label": "Yes please"}],
        )

        assert result.document_errors == []
