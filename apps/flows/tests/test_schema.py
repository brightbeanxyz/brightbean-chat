"""The registry, the handle grammar, and the fixtures Layer 3 will build on."""

import pytest

from apps.flows.fixtures import NODE_CONFIGS, graph_for, valid_graphs
from apps.flows.schema import (
    ACTION_VERBS,
    NODE_TYPES,
    handles_for_node,
    json_schema,
    node_spec,
    parse_handle,
    register_node_type,
    validate_graph,
)
from apps.flows.schema.jsonschema import validate_instance
from apps.flows.schema.nodes import NodeSpec, all_defs

# SPEC §11's eleven node types. Written out rather than derived from the
# registry: a test that asks the registry what it contains cannot notice a type
# going missing.
SPEC_NODE_TYPES = {
    "send_message",
    "action",
    "start_flow",
    "condition",
    "smart_delay",
    "randomizer",
    "external_request",
    "data_collection",
    "send_sms",
    "send_email",
    "note",
}


class TestRegistry:
    def test_every_spec_node_type_ships_now(self):
        """ROADMAP contract 2: L3-B and L3-C both build on this, so the types
        whose runtime lands later still need their schema today."""
        assert set(NODE_TYPES) == SPEC_NODE_TYPES

    def test_every_spec_action_verb_ships_now(self):
        assert set(ACTION_VERBS) == {
            "add_tag",
            "remove_tag",
            "set_field",
            "clear_field",
            "subscribe_sequence",
            "unsubscribe_sequence",
            "open_conversation",
            "close_conversation",
            "assign_conversation",
            "notify_members",
        }

    def test_registering_a_type_twice_is_an_error(self):
        with pytest.raises(ValueError, match="already registered"):
            register_node_type(NodeSpec(type="note", label="Note", description="", config={}))

    def test_an_unknown_type_has_no_spec(self):
        assert node_spec("send_smoke_signal") is None
        assert node_spec(None) is None

    def test_every_config_object_rejects_unknown_keys(self):
        """The mass-assignment guard is structural: fields.obj() always closes
        the object, so this holds for every fragment in the document."""
        for name, schema in all_defs().items():
            if schema.get("type") == "object" and "properties" in schema:
                assert schema.get("additionalProperties") is False, name


class TestHandles:
    @pytest.mark.parametrize(
        "raw",
        ["default", "timeout", "error", "cond:true", "cond:false", "btn:b1", "qr:q_1", "rand:path-a"],
    )
    def test_the_grammar_accepts_what_spec_9_1_lists(self, raw):
        assert parse_handle(raw) is not None

    @pytest.mark.parametrize(
        "raw",
        ["", "Default", "cond:maybe", "btn:", "btn:a:b", "btn:has space", "qr:" + "x" * 65, "onwards", None, 1],
    )
    def test_the_grammar_rejects_everything_else(self, raw):
        assert parse_handle(raw) is None

    def test_dynamic_handles_come_from_the_config(self):
        spec = NODE_TYPES["send_message"]

        assert handles_for_node(spec, NODE_CONFIGS["send_message"]) == {
            "default",
            "timeout",
            "btn:b1",
            "btn:b2",
            "qr:q1",
        }

    def test_a_randomizer_exposes_only_its_paths(self):
        assert handles_for_node(NODE_TYPES["randomizer"], NODE_CONFIGS["randomizer"]) == {"rand:a", "rand:b"}

    def test_a_terminal_node_exposes_nothing(self):
        assert handles_for_node(NODE_TYPES["start_flow"], NODE_CONFIGS["start_flow"]) == set()


class TestFixtures:
    def test_there_is_a_valid_graph_for_every_node_type(self):
        graphs = valid_graphs()

        assert set(graphs) == SPEC_NODE_TYPES
        for node_type, graph in graphs.items():
            result = validate_graph(graph)
            assert result.errors == [], (node_type, result.as_dict())
            assert result.warnings == [], (node_type, result.as_dict())

    def test_a_fixture_wires_up_every_handle_its_node_exposes(self):
        graph = graph_for("send_message")

        assert {edge["sourceHandle"] for edge in graph["edges"]} == {
            "default",
            "timeout",
            "btn:b1",
            "btn:b2",
            "qr:q1",
        }

    def test_the_fixtures_are_independent_copies(self):
        """L3-B will mutate these. A shared nested dict would let one engine
        test change what the next one validates."""
        first = graph_for("send_message")
        first["nodes"][0]["config"]["blocks"][0]["text"] = "changed"

        assert graph_for("send_message")["nodes"][0]["config"]["blocks"][0]["text"] != "changed"


class TestTheExportedDocumentAgrees:
    """The hand-written envelope checks and the exported schema describe one
    format. They are written separately — one for precise error addressing, one
    for the client — so something has to hold them together."""

    def test_every_valid_fixture_also_validates_against_the_exported_schema(self):
        document = json_schema()
        defs = document["$defs"]

        for node_type, graph in valid_graphs().items():
            issues = validate_instance(document, graph, path="", defs=defs)
            assert issues == [], (node_type, [issue.as_dict() for issue in issues])

    def test_the_exported_schema_rejects_what_the_server_rejects(self):
        document = json_schema()
        graph = graph_for("send_sms")
        graph["nodes"][0]["config"]["from_number"] = "+15550000"

        issues = validate_instance(document, graph, path="", defs=document["$defs"])

        assert [issue.code for issue in issues] == ["unknown_config_key"]

    def test_every_ref_in_the_document_resolves(self):
        document = json_schema()
        names = set(document["$defs"])
        missing: list[str] = []

        def walk(node: object) -> None:
            if isinstance(node, dict):
                ref = node.get("$ref")
                if isinstance(ref, str) and ref.rsplit("/", 1)[-1] not in names:
                    missing.append(ref)
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(document)
        assert missing == []


class TestExtensionPoints:
    """The registries later issues append to (contracts 2 and 5)."""

    def test_a_verb_registered_later_appears_in_the_schema_and_validates(self):
        from apps.flows.schema import fields as f
        from apps.flows.schema.nodes import ACTION_VERBS, SHARED_DEFS, register_action_verb

        register_action_verb(
            "pause_automation",
            "action_pause_automation",
            f.obj({"verb": f.const("pause_automation"), "minutes": f.integer(minimum=1)}, required=["verb", "minutes"]),
        )
        try:
            graph = graph_for("action")
            graph["nodes"][0]["config"]["actions"] = [{"verb": "pause_automation", "minutes": 30}]

            assert validate_graph(graph).errors == []
            assert "action_pause_automation" in json_schema()["$defs"]
        finally:
            del ACTION_VERBS["pause_automation"]
            del SHARED_DEFS["action_pause_automation"]

    def test_registering_a_verb_twice_is_an_error(self):
        from apps.flows.schema.nodes import register_action_verb

        with pytest.raises(ValueError, match="already registered"):
            register_action_verb("add_tag", "action_add_tag_again", {})

    def test_redefining_a_shared_fragment_is_an_error(self):
        from apps.flows.schema.nodes import register_defs

        with pytest.raises(ValueError, match="already registered"):
            register_defs(quick_reply={"type": "string"})


class TestTheConditionDefsHoist:
    """Issue #3's CONDITION_SCHEMA may arrive carrying its own ``$defs``. A
    ``$ref`` inside an embedded fragment resolves against the root of the
    document it lands in, so those have to be lifted or every one of them
    dangles."""

    def test_nested_defs_are_lifted_to_the_document_root(self, monkeypatch):
        from apps.flows.schema import nodes

        monkeypatch.setitem(
            nodes.SHARED_DEFS,
            "condition_filter",
            {
                "type": "object",
                "properties": {"rule": {"$ref": "#/$defs/imported_rule"}},
                "$defs": {"imported_rule": {"type": "string"}},
            },
        )
        defs = nodes.all_defs()

        assert defs["imported_rule"] == {"type": "string"}
        assert "$defs" not in defs["condition_filter"]

    def test_a_name_collision_is_refused_rather_than_overwritten(self, monkeypatch):
        from apps.flows.schema import nodes

        monkeypatch.setitem(
            nodes.SHARED_DEFS,
            "condition_filter",
            {"type": "object", "$defs": {"quick_reply": {"type": "string"}}},
        )

        with pytest.raises(ValueError, match="collides"):
            nodes.all_defs()


class TestTheJsonSchemaInterpreter:
    def test_a_dangling_ref_is_treated_as_unconstrained_not_a_crash(self):
        """A broken $ref is a bug in this repository's schemas, caught by the
        export test — not something to raise on in a request path."""
        assert validate_instance({"$ref": "#/$defs/nope"}, {"anything": 1}, path="", defs={}) == []

    def test_one_of_requires_exactly_one_match_unlike_any_of(self):
        """The interpreter exists to read fragments this app did not write —
        CONDITION_SCHEMA arrives from apps.contacts (contract 8). Treating
        `oneOf` as `anyOf` would let a value matching two branches through."""
        branches = [
            {"type": "object", "properties": {"a": {"type": "string"}}},
            {"type": "object", "properties": {"b": {"type": "string"}}},
        ]
        both = {"a": "x", "b": "y"}

        one_of = validate_instance({"oneOf": branches}, both, path="cfg", defs={})
        any_of = validate_instance({"anyOf": branches}, both, path="cfg", defs={})

        assert [issue.code for issue in one_of] == ["invalid_config_value"]
        assert "exactly one" in one_of[0].message
        assert any_of == []

    def test_one_of_still_passes_a_value_matching_a_single_branch(self):
        branches = [
            {"type": "object", "required": ["a"], "properties": {"a": {"type": "string"}}},
            {"type": "object", "required": ["b"], "properties": {"b": {"type": "string"}}},
        ]

        assert validate_instance({"oneOf": branches}, {"a": "x"}, path="cfg", defs={}) == []

    def test_a_variant_union_without_a_discriminator_reports_the_near_miss(self):
        schema = {
            "anyOf": [
                {"type": "object", "required": ["a"], "properties": {"a": {"type": "string"}}},
                {
                    "type": "object",
                    "required": ["b", "c", "d"],
                    "properties": {"b": {"type": "string"}, "c": {"type": "string"}, "d": {"type": "string"}},
                },
            ]
        }

        issues = validate_instance(schema, {"a": 1}, path="cfg", defs={})

        assert [issue.code for issue in issues] == ["invalid_config_value"]

    def test_a_pattern_is_enforced(self):
        graph = graph_for("smart_delay")
        graph["nodes"][0]["config"]["continue_window"]["from"] = "9am"

        assert [issue.code for issue in validate_graph(graph).errors] == ["invalid_config_value"]

    def test_one_wrong_type_does_not_cascade(self):
        """Reporting every keyword that could not be checked after a type
        mismatch buries the one finding that matters."""
        issues = validate_instance(
            {"type": "array", "minItems": 2, "items": {"type": "string"}}, "not a list", path="cfg", defs={}
        )

        assert len(issues) == 1


class TestTheExportIsACopy:
    def test_mutating_the_exported_document_cannot_touch_the_registry(self):
        """Every fragment used to be the same object the validator reads, so the
        first caller to adjust one for rendering would rewrite the rules the
        server enforces for the life of the process."""
        from apps.flows.schema.nodes import SHARED_DEFS

        before = SHARED_DEFS["quick_reply"]["properties"]["label"]["maxLength"]
        document = json_schema()
        document["$defs"]["quick_reply"]["properties"]["label"]["maxLength"] = 1
        document["$defs"]["node_send_sms"]["properties"]["config"]["properties"]["text"]["maxLength"] = 1

        assert SHARED_DEFS["quick_reply"]["properties"]["label"]["maxLength"] == before
        assert json_schema()["$defs"]["quick_reply"]["properties"]["label"]["maxLength"] == before

    def test_the_limits_have_one_source(self):
        from apps.flows.schema import limits

        assert json_schema()["x-brightbean"]["limits"] == limits()


class TestHandleFormatting:
    def test_a_parsed_handle_round_trips(self):
        from apps.flows.schema.handles import format_handle

        assert str(parse_handle("btn:b1")) == "btn:b1"
        assert str(parse_handle("default")) == "default"
        assert format_handle("cond", "true") == "cond:true"
