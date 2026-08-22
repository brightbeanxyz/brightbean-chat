"""Per-type config schemas, and the caps that run before them."""

import pytest

from apps.flows.models import TriggerType
from apps.flows.triggers.registry import TRIGGER_TYPES, spec_for
from apps.flows.triggers.schema import trigger_json_schema
from apps.flows.triggers.validation import (
    MAX_TRIGGER_CONFIG_BYTES,
    MAX_TRIGGER_CONFIG_DEPTH,
    validate_config,
)


def _codes(issues):
    return {issue.code for issue in issues}


class TestTheRegistry:
    def test_every_trigger_type_has_a_spec(self):
        """A type with no spec cannot be validated, rendered or platform-checked."""
        assert set(TRIGGER_TYPES) == set(TriggerType.values)

    def test_every_schema_refuses_unknown_keys(self):
        """SECURITY-BASELINE §7's mass-assignment guard, checked type by type
        rather than trusted to obj()'s default."""
        for spec in TRIGGER_TYPES.values():
            assert spec.config.get("additionalProperties") is False, spec.type

    #: Types whose default config is deliberately *not* savable: the form opens
    #: on it, and the user has to supply the one thing that makes the trigger
    #: mean anything. A keyword trigger with no keywords and a ref trigger with
    #: no ref would both match nothing, silently, for ever.
    NEEDS_INPUT = {TriggerType.KEYWORD, TriggerType.REF_URL, TriggerType.RULE}

    def test_every_default_config_is_savable_unless_it_needs_input(self):
        """ "Add a trigger" must not start from something the validator refuses,
        except where refusing is the point."""
        for spec in TRIGGER_TYPES.values():
            if spec.type in self.NEEDS_INPUT:
                continue
            assert validate_config(spec.type, spec.default_config()) == [], spec.type

    def test_the_types_that_need_input_refuse_their_own_default(self):
        """The other half of the pair above, so NEEDS_INPUT cannot quietly grow."""
        for trigger_type in self.NEEDS_INPUT:
            spec = spec_for(trigger_type)
            assert validate_config(trigger_type, spec.default_config()) != [], trigger_type

    def test_the_two_unbindable_types_are_the_ones_no_channel_delivers(self):
        assert not spec_for(TriggerType.RULE).bindable
        assert not spec_for(TriggerType.API).bindable
        assert spec_for(TriggerType.KEYWORD).bindable

    def test_the_json_schema_document_covers_every_type(self):
        document = trigger_json_schema()
        assert set(document["triggerTypes"]) == set(TriggerType.values)
        assert "$defs" in document


class TestKeywordConfig:
    def test_a_valid_config_passes(self):
        config = {"keywords": [{"text": "help", "mode": "exact"}]}
        assert validate_config(TriggerType.KEYWORD, config) == []

    def test_an_unknown_key_is_refused(self):
        config = {"keywords": [{"text": "help", "mode": "exact"}], "sneaky": True}
        assert "unknown_config_key" in _codes(validate_config(TriggerType.KEYWORD, config))

    def test_a_missing_required_key_is_refused(self):
        assert "missing_required_config" in _codes(validate_config(TriggerType.KEYWORD, {}))

    def test_an_unknown_mode_is_refused(self):
        config = {"keywords": [{"text": "help", "mode": "fuzzy"}]}
        assert "invalid_config_value" in _codes(validate_config(TriggerType.KEYWORD, config))

    def test_an_empty_keyword_list_is_refused(self):
        """SPEC §10's keyword trigger with no keywords would match nothing, for ever."""
        assert validate_config(TriggerType.KEYWORD, {"keywords": []}) != []


class TestRefConfig:
    @pytest.mark.parametrize("ref", ["spring-promo", "a", "A_1-b", "x" * 64])
    def test_an_acceptable_ref_passes(self, ref):
        assert validate_config(TriggerType.REF_URL, {"ref": ref}) == []

    @pytest.mark.parametrize("ref", ["with space", "slash/es", "quest?ion", "x" * 65, "emoji-🙂", ""])
    def test_an_unsafe_ref_is_refused(self, ref):
        """REF_PATTERN is what lets the link and the QR carry the ref unencoded,
        so anything that would need escaping has to be refused at the door."""
        assert validate_config(TriggerType.REF_URL, {"ref": ref}) != []


class TestCaps:
    def test_an_oversized_config_is_refused_before_the_schema_walk(self):
        config = {"keywords": [{"text": "x" * 199, "mode": "exact"} for _ in range(200)]}
        issues = validate_config(TriggerType.KEYWORD, config)

        assert len(issues) == 1
        assert str(MAX_TRIGGER_CONFIG_BYTES) in issues[0].message

    def test_a_too_deep_config_is_refused(self):
        nested = {"keywords": []}
        cursor = nested
        for _ in range(MAX_TRIGGER_CONFIG_DEPTH + 2):
            cursor["deeper"] = {}
            cursor = cursor["deeper"]

        issues = validate_config(TriggerType.KEYWORD, nested)
        assert len(issues) == 1
        assert "deeper than" in issues[0].message

    def test_a_config_that_is_not_an_object_is_refused(self):
        assert validate_config(TriggerType.KEYWORD, ["nope"]) != []

    def test_an_unknown_trigger_type_is_refused(self):
        assert validate_config("teleport", {}) != []


class TestRuleConfig:
    def test_it_reuses_the_shared_condition_schema(self):
        """L6-A adds a binding, not a schema — so `filters` has to validate
        against the condition filter the condition node already registered."""
        config = {"event": "tag_added", "filters": {"match": "all", "rules": []}}
        assert validate_config(TriggerType.RULE, config) == []

    def test_an_unknown_event_is_refused(self):
        assert validate_config(TriggerType.RULE, {"event": "sneezed"}) != []
