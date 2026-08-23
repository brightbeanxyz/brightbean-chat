"""ROADMAP contract 5, and the agreement between runtime and schema.

Two registries, two apps, one node type. The tests here are mostly about the
*seam*: a runtime whose type no schema describes can never run, and a schema
whose type no runtime claims cannot either. Both are legal states of a layered
build — the second one is the normal state — so the point is to make each a
deliberate, visible fact rather than something noticed in production.
"""

import pytest

from apps.flows.engine import (
    DuplicateNodeTypeError,
    DuplicateVerbError,
    node_class_for,
    register_node,
    register_verb,
    registered_node_types,
    registered_verbs,
    synchronous_safe,
    types_without_runtime,
    verb_handler,
)
from apps.flows.engine.nodes.base import Node
from apps.flows.schema import ACTION_VERBS, NODE_TYPES, handles_for_node, node_spec
from apps.flows.tests.support import node_runtime

#: Node types whose schema ships in L2-D and whose runtime is somebody else's.
#:
#: Pinned so a type joining or leaving the set is a line in a diff.
#: ``external_request`` left it in L4-E and ``send_sms`` in L5-D (#20);
#: ``send_email`` is L5-E's and is the last one.
EXPECTED_WITHOUT_RUNTIME = {"send_email"}

#: SPEC §7.1's inline-safe set, verbatim: "send message, action, condition,
#: randomizer, start flow". ``note`` joins it for free — it does nothing.
#:
#: ``smart_delay`` and ``data_collection`` are deliberately *absent*: SPEC names
#: the safe five and neither is one of them. Nothing is lost by enqueueing them
#: — a delay is not the first reply a webhook is racing to produce, and a
#: question sends and then parks.
EXPECTED_SYNCHRONOUS_SAFE = {"action", "condition", "randomizer", "send_message", "start_flow", "note"}


class TestRuntimeMatchesSchema:
    def test_every_runtime_node_type_has_a_schema(self):
        for node_type in registered_node_types():
            assert node_spec(node_type) is not None, f"{node_type} has a runtime and no NodeSpec"

    def test_the_types_still_waiting_for_a_runtime_are_the_expected_ones(self):
        assert set(types_without_runtime()) == EXPECTED_WITHOUT_RUNTIME

    def test_every_schema_type_is_accounted_for(self):
        assert set(NODE_TYPES) == set(registered_node_types()) | EXPECTED_WITHOUT_RUNTIME

    def test_every_runtime_verb_has_a_schema(self):
        for verb in registered_verbs():
            assert verb in ACTION_VERBS, f"{verb} has a runtime and no schema"

    def test_the_sequence_verbs_have_a_schema_and_no_runtime(self):
        """L6-A's, deliberately left out (see the #9 trigger)."""
        for verb in ("subscribe_sequence", "unsubscribe_sequence"):
            assert verb in ACTION_VERBS
            assert verb_handler(verb) is None


class TestHandles:
    """A node may only ask for a handle its own spec exposes."""

    def test_the_condition_node_returns_handles_the_spec_declares(self):
        available = handles_for_node(node_spec("condition"), {})
        assert {"cond:true", "cond:false"} <= available

    def test_the_randomizer_derives_its_handles_from_config(self):
        config = {"paths": [{"id": "a", "weight": 50}, {"id": "b", "weight": 50}]}
        assert handles_for_node(node_spec("randomizer"), config) == {"rand:a", "rand:b"}

    def test_the_start_flow_node_exposes_no_handles(self):
        assert handles_for_node(node_spec("start_flow"), {}) == set()


class TestSynchronousSafety:
    def test_the_inline_safe_set_is_spec_seven_ones(self):
        safe = {node_type for node_type in registered_node_types() if synchronous_safe(node_type)}
        assert safe == EXPECTED_SYNCHRONOUS_SAFE

    def test_a_type_with_no_runtime_is_never_inline_safe(self):
        """Something that cannot run at all cannot run inside a 1.5-second budget."""
        for node_type in types_without_runtime():
            assert synchronous_safe(node_type) is False

    def test_an_unknown_type_is_not_inline_safe(self):
        assert synchronous_safe("no-such-node") is False


class TestRegistrationRules:
    def test_a_runtime_without_a_schema_is_refused(self):
        class Orphan(Node):
            type = "not_a_real_node_type"

            def execute(self, ctx):  # pragma: no cover - never reached
                raise AssertionError

        with pytest.raises(ValueError, match="has no NodeSpec"):
            register_node(Orphan)

    def test_a_class_with_no_type_is_refused(self):
        class Nameless(Node):
            def execute(self, ctx):  # pragma: no cover - never reached
                raise AssertionError

        with pytest.raises(ValueError, match="has no `type`"):
            register_node(Nameless)

    def test_a_second_runtime_for_one_type_raises(self):
        class Rival(Node):
            type = "note"

            def execute(self, ctx):  # pragma: no cover - never reached
                raise AssertionError

        with pytest.raises(DuplicateNodeTypeError, match="already run by"):
            register_node(Rival)

    def test_replace_is_the_deliberate_override(self):
        original = node_class_for("note")
        with node_runtime("note", lambda ctx: None):
            assert node_class_for("note") is not original
        assert node_class_for("note") is original

    def test_a_verb_without_a_schema_is_refused(self):
        with pytest.raises(ValueError, match="has no schema"):
            register_verb("teleport_contact", lambda ctx, step: None)

    def test_a_second_runtime_for_one_verb_raises(self):
        with pytest.raises(DuplicateVerbError, match="already handled by"):
            register_verb("add_tag", lambda ctx, step: None)
