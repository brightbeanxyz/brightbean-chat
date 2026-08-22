"""The generated artefact: deterministic, committed, and never quietly stale."""

import json

import pytest
from django.core.management import CommandError, call_command

from apps.flows.schema import artifact_path, json_schema, serialize
from apps.flows.schema.condition import CONDITION_SCHEMA, CONDITION_SCHEMA_IS_VENDORED
from apps.flows.schema.nodes import GROUPS


class TestDeterminism:
    def test_two_runs_produce_identical_bytes(self):
        assert serialize() == serialize()

    def test_the_output_is_sorted_and_newline_terminated(self):
        text = serialize()

        assert text.endswith("\n")
        assert json.dumps(json.loads(text), indent=2, sort_keys=True, ensure_ascii=False) + "\n" == text


class TestTheCommittedArtefact:
    def test_it_matches_the_registry(self):
        """The staleness gate. `make frontend` regenerates it and the builder
        imports it at build time, so a registry change that forgets to run the
        generator would otherwise ship panels describing last week's schema."""
        path = artifact_path()

        assert path.exists(), f"{path} is missing — run `make schema`."
        assert path.read_text(encoding="utf-8") == serialize(), (
            f"{path} is out of date. Run `make schema` and commit the result."
        )

    def test_it_is_the_document_the_api_serves(self):
        assert json.loads(artifact_path().read_text(encoding="utf-8")) == json_schema()

    def test_it_carries_what_the_builder_needs_beyond_validation(self):
        extras = json_schema()["x-brightbean"]

        assert extras["schema_version"] == 1
        assert {entry["type"] for entry in extras["node_types"]} == set(
            json_schema()["$defs"]["node"]["discriminator"]["mapping"]
        )
        assert extras["limits"]["max_nodes"] == 500

    def test_every_node_type_declares_a_palette_group(self):
        """Issue #10's palette is generated, so a node type with no group — or
        one naming a drawer that does not exist — would silently vanish from it."""
        extras = json_schema()["x-brightbean"]
        known = {key for key, _ in GROUPS}

        for entry in extras["node_types"]:
            assert entry["group"] in known, f"{entry['type']} declares unknown group {entry['group']!r}"

    def test_the_palette_groups_ship_ordered_with_their_labels(self):
        """Order and copy are data too, so the builder needs no second table."""
        assert json_schema()["x-brightbean"]["groups"] == [{"key": key, "label": label} for key, label in GROUPS]

    def test_every_group_but_the_fallback_has_a_node_type_in_it(self):
        """`other` is the default a later issue's node type falls into, so it is
        allowed to be empty. A named drawer that is empty is a typo."""
        used = {entry["group"] for entry in json_schema()["x-brightbean"]["node_types"]}

        assert {key for key, _ in GROUPS} - used == {"other"}


class TestTheManagementCommand:
    def test_check_passes_against_the_committed_file(self):
        call_command("export_flow_schema", "--check")

    def test_check_fails_when_the_file_is_stale(self, tmp_path, monkeypatch):
        stale = tmp_path / "flow-schema.json"
        stale.write_text("{}\n", encoding="utf-8")
        monkeypatch.setattr("apps.flows.management.commands.export_flow_schema.artifact_path", lambda: stale)

        with pytest.raises(CommandError, match="out of date"):
            call_command("export_flow_schema", "--check")

    def test_check_fails_when_the_file_is_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "apps.flows.management.commands.export_flow_schema.artifact_path",
            lambda: tmp_path / "nothing.json",
        )

        with pytest.raises(CommandError, match="out of date"):
            call_command("export_flow_schema", "--check")

    def test_it_writes_the_file_and_creates_the_directory(self, tmp_path, monkeypatch):
        target = tmp_path / "generated" / "flow-schema.json"
        monkeypatch.setattr("apps.flows.management.commands.export_flow_schema.artifact_path", lambda: target)

        call_command("export_flow_schema")

        assert target.read_text(encoding="utf-8") == serialize()


class TestTheConditionSwapPoint:
    """ROADMAP contract 8. #3 owns CONDITION_SCHEMA; this app embeds it."""

    def test_the_condition_node_uses_whichever_schema_is_in_force(self):
        assert json_schema()["$defs"]["condition_filter"] == CONDITION_SCHEMA
        assert json_schema()["$defs"]["node_condition"]["properties"]["config"] == {"$ref": "#/$defs/condition_filter"}

    def test_the_swap_point_reports_which_schema_is_in_force(self):
        """Passes either way, and changes meaning the moment #3 merges: if the
        import is available but did not take, that is a red build rather than a
        surprise in Layer 3."""
        try:
            from apps.contacts.conditions import CONDITION_SCHEMA as CONTACTS_SCHEMA
        except ImportError:
            assert CONDITION_SCHEMA_IS_VENDORED is True
            assert CONDITION_SCHEMA["properties"]["match"]["enum"] == ["all", "any"]
        else:
            assert CONDITION_SCHEMA_IS_VENDORED is False
            assert CONDITION_SCHEMA is CONTACTS_SCHEMA

    def test_the_vendored_form_carries_every_operator_spec_11_4_lists(self):
        if not CONDITION_SCHEMA_IS_VENDORED:  # pragma: no cover - once #3 has merged
            pytest.skip("apps.contacts.conditions owns the operator table now.")

        operators = set(CONDITION_SCHEMA["properties"]["rules"]["items"]["properties"]["op"]["enum"])

        assert operators == {
            "is",
            "is_not",
            "contains",
            "has_value",
            "no_value",
            "=",
            "!=",
            ">",
            "<",
            ">=",
            "<=",
            "before",
            "after",
            "on",
            "days_ago",
            "days_from_now",
            "has",
            "has_not",
            "subscribed",
            "not_subscribed",
            "inside",
            "outside",
        }

    def test_the_sources_are_the_registry_contract_8_describes(self):
        if not CONDITION_SCHEMA_IS_VENDORED:  # pragma: no cover - once #3 has merged
            pytest.skip("apps.contacts.conditions owns the source registry now.")

        sources = set(CONDITION_SCHEMA["properties"]["rules"]["items"]["properties"]["source"]["enum"])

        assert sources == {"tag", "custom_field", "system_field", "segment", "sequence", "window"}
