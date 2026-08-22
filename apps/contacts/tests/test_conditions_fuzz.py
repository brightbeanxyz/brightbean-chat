"""Hostile ``filter_json``: everything must be refused before it reaches the ORM.

The mechanism, not just the outcome, is what is asserted. Structural and
vocabulary failures are checked with a query capture that must stay **empty** —
a hostile ``key`` or ``op`` never gets as far as a lookup, let alone a query
kwarg (SECURITY-BASELINE §7). Reference failures (an id from another workspace)
legitimately cost one scoped lookup, and are checked for saying "unknown" rather
than "forbidden", which is SECURITY-BASELINE §1's no-existence-oracle rule
applied to a filter document.
"""

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.contacts import services
from apps.contacts.conditions import (
    MAX_FILTER_BYTES,
    MAX_RULES,
    MAX_VALUE_CHARS,
    ConditionValidationError,
    queryset,
    validate,
)
from apps.contacts.models import CustomFieldType, Segment

UUID_A = "8f14e45f-ceea-467a-9f52-9d9c9d9c9d9c"


def rule(**overrides):
    base = {"source": "system_field", "key": "email", "op": "is", "value": "x"}
    base.update(overrides)
    return {"match": "all", "rules": [base]}


#: (id, payload, expected error code). Every one must be refused with **no SQL**.
PRE_ORM_PAYLOADS: tuple[tuple[str, object, str], ...] = (
    # -- document shape ------------------------------------------------------
    ("root-not-an-object", [], "not_an_object"),
    ("root-is-a-string", "all", "bad_json"),
    ("root-is-null", None, "not_an_object"),
    ("root-extra-key", {"match": "all", "rules": [], "extra": 1}, "unknown_key"),
    ("root-missing-rules", {"match": "all"}, "missing_key"),
    ("root-missing-match", {"rules": []}, "missing_key"),
    ("match-is-case-sensitive", {"match": "ALL", "rules": []}, "bad_match"),
    ("rules-not-a-list", {"match": "all", "rules": {}}, "not_a_list"),
    (
        "too-many-rules",
        {"match": "all", "rules": [{"source": "tag", "key": UUID_A, "op": "has"}] * (MAX_RULES + 1)},
        "too_many_rules",
    ),
    # -- mass assignment -----------------------------------------------------
    ("rule-carries-a-workspace", rule(workspace=UUID_A), "unknown_key"),
    (
        "rule-carries-a-dunder",
        {"match": "all", "rules": [{"__class__": {}, "source": "tag", "key": UUID_A, "op": "has"}]},
        "unknown_key",
    ),
    ("rule-missing-op", {"match": "all", "rules": [{"source": "tag", "key": UUID_A}]}, "missing_key"),
    # -- vocabulary ----------------------------------------------------------
    ("unknown-source", rule(source="astrology"), "unknown_source"),
    ("source-is-a-dunder", rule(source="__init__"), "unknown_source"),
    ("unknown-op", rule(op="regex"), "unknown_op"),
    (
        "op-not-legal-for-source",
        {"match": "all", "rules": [{"source": "tag", "key": UUID_A, "op": "contains", "value": "x"}]},
        "op_not_legal_for_source",
    ),
    # -- key smuggling -------------------------------------------------------
    ("key-smuggles-a-lookup", rule(key="email__regex"), "unknown_key"),
    ("key-traverses-a-relation", rule(key="workspace__organization__name"), "unknown_key"),
    ("key-is-a-tenancy-column", rule(key="workspace_id"), "unknown_key"),
    ("key-is-the-soft-delete-flag", rule(key="status"), "unknown_key"),
    ("key-is-sql", {"match": "all", "rules": [{"source": "tag", "key": "' OR 1=1 --", "op": "has"}]}, "bad_uuid"),
    ("key-is-a-list", rule(key=["a", "b"]), "bad_key_type"),
    ("key-is-a-number", rule(key=5), "bad_key_type"),
    ("key-is-empty", rule(key=""), "bad_key"),
    (
        "unknown-platform-for-window",
        {"match": "all", "rules": [{"source": "window", "key": "carrier-pigeon", "op": "inside"}]},
        "unknown_key",
    ),
    # -- values --------------------------------------------------------------
    (
        "valueless-op-carrying-a-value",
        {"match": "all", "rules": [{"source": "tag", "key": UUID_A, "op": "has", "value": "x"}]},
        "value_not_allowed",
    ),
    (
        "valueless-op-carrying-null",
        {"match": "all", "rules": [{"source": "tag", "key": UUID_A, "op": "has", "value": None}]},
        "value_not_allowed",
    ),
    (
        "comparison-op-missing-its-value",
        {"match": "all", "rules": [{"source": "system_field", "key": "email", "op": "is"}]},
        "value_required",
    ),
    ("text-value-is-a-number", rule(value=5), "bad_value_type"),
    ("text-value-holds-a-nul-byte", rule(value="a\x00b"), "nul_byte"),
    ("text-value-is-too-long", rule(value="x" * 5000), "value_too_long"),
    # -- dates ---------------------------------------------------------------
    ("date-is-impossible", rule(key="last_interaction_at", op="on", value="2026-02-30"), "bad_date"),
    ("date-is-prose", rule(key="last_interaction_at", op="on", value="tomorrow"), "bad_date"),
    ("date-carries-a-time", rule(key="last_interaction_at", op="on", value="2026-08-21T00:00:00Z"), "bad_date"),
    (
        "relative-unit-is-unknown",
        rule(key="last_interaction_at", op="on", value={"relative": {"unit": "years", "offset": 1}}),
        "bad_unit",
    ),
    (
        "relative-offset-is-absurd",
        rule(key="last_interaction_at", op="on", value={"relative": {"unit": "days", "offset": 99999999}}),
        "offset_out_of_range",
    ),
    (
        "relative-offset-is-a-bool",
        rule(key="last_interaction_at", op="on", value={"relative": {"unit": "days", "offset": True}}),
        "bad_value_type",
    ),
    (
        "relative-has-an-extra-key",
        rule(key="last_interaction_at", op="on", value={"relative": {"unit": "days", "offset": 1, "x": 2}}),
        "unknown_key",
    ),
    ("relative-is-empty", rule(key="last_interaction_at", op="on", value={"relative": {}}), "missing_key"),
    # -- nesting and size ----------------------------------------------------
    ("value-is-a-deep-list", rule(value=[[[[[[[[1]]]]]]]]), "too_deep"),
    # Deliberately under MAX_FILTER_BYTES, so it is the depth guard that fires
    # and not the size cap — the bracket prescan is what stops the decoder
    # raising RecursionError inside a request.
    ("raw-json-is-a-depth-bomb", '{"a":' * 2000 + "null" + "}" * 2000, "too_deep"),
    ("raw-json-is-oversized", '{"match":"all","rules":[],"pad":"' + "x" * MAX_FILTER_BYTES + '"}', "too_large"),
    ("raw-json-is-malformed", "{not json", "bad_json"),
    # json.loads raises a bare ValueError, not a JSONDecodeError, past
    # CPython 3.11's 4300-digit int conversion limit — and 5 KB of digits
    # sits comfortably inside MAX_FILTER_BYTES, so the size cap does not
    # catch it. Escaping as a ValueError would be a 500 rather than a 400.
    (
        "raw-json-holds-an-oversized-integer",
        '{"match":"all","rules":[{"source":"system_field","key":"email","op":"is","value":' + "9" * 5000 + "}]}",
        "bad_json",
    ),
    (
        "raw-json-holds-nan",
        '{"match":"all","rules":[{"source":"system_field","key":"email","op":"is","value":NaN}]}',
        "bad_number",
    ),
)


@pytest.mark.django_db
class TestHostileDocumentsNeverReachTheOrm:
    @pytest.mark.parametrize(
        ("payload", "code"), [(p, c) for _, p, c in PRE_ORM_PAYLOADS], ids=[i for i, _, _ in PRE_ORM_PAYLOADS]
    )
    def test_it_is_refused_before_a_single_query_runs(self, workspace, payload, code):
        with CaptureQueriesContext(connection) as captured, pytest.raises(ConditionValidationError) as exc:
            validate(workspace, payload)

        assert captured.captured_queries == []
        assert exc.value.code == code

    @pytest.mark.parametrize(
        ("payload", "code"), [(p, c) for _, p, c in PRE_ORM_PAYLOADS], ids=[i for i, _, _ in PRE_ORM_PAYLOADS]
    )
    def test_the_error_names_a_path_a_builder_can_highlight(self, workspace, payload, code):
        with pytest.raises(ConditionValidationError) as exc:
            validate(workspace, payload)

        assert isinstance(exc.value.path, str)
        assert exc.value.code == code


@pytest.mark.django_db
class TestCrossTenantReferencesAreUnknownNotForbidden:
    def test_another_workspaces_tag(self, workspace, other_tenancy):
        theirs, _ = services.get_or_create_tag(other_tenancy.workspace, "theirs")

        with pytest.raises(ConditionValidationError) as exc:
            validate(workspace, {"match": "all", "rules": [{"source": "tag", "key": str(theirs.pk), "op": "has"}]})

        assert exc.value.code == "unknown_tag"

    def test_a_nonexistent_tag_reports_the_same_thing(self, workspace):
        with pytest.raises(ConditionValidationError) as exc:
            validate(workspace, {"match": "all", "rules": [{"source": "tag", "key": UUID_A, "op": "has"}]})

        assert exc.value.code == "unknown_tag"

    def test_another_workspaces_custom_field(self, workspace, other_tenancy):
        theirs = services.create_custom_field(other_tenancy.workspace, name="Plan", field_type=CustomFieldType.TEXT)
        payload = {
            "match": "all",
            "rules": [{"source": "custom_field", "key": str(theirs.pk), "op": "is", "value": "x"}],
        }

        with pytest.raises(ConditionValidationError) as exc:
            validate(workspace, payload)

        assert exc.value.code == "unknown_field"

    def test_an_operator_illegal_for_the_fields_type_is_refused(self, workspace):
        number = services.create_custom_field(workspace, name="Plan", field_type=CustomFieldType.NUMBER)
        payload = {
            "match": "all",
            "rules": [{"source": "custom_field", "key": str(number.pk), "op": "contains", "value": "x"}],
        }

        with pytest.raises(ConditionValidationError) as exc:
            validate(workspace, payload)

        assert exc.value.code == "op_type_mismatch"

    def test_a_bool_is_not_accepted_where_a_number_belongs(self, workspace):
        """isinstance(True, int) is True in Python: without the explicit check,
        `> true` would compile to `value_number > 1`."""
        number = services.create_custom_field(workspace, name="Plan", field_type=CustomFieldType.NUMBER)
        payload = {
            "match": "all",
            "rules": [{"source": "custom_field", "key": str(number.pk), "op": ">", "value": True}],
        }

        with pytest.raises(ConditionValidationError) as exc:
            validate(workspace, payload)

        assert exc.value.code == "bad_value_type"


@pytest.mark.django_db
class TestSegmentExpansionIsBounded:
    def test_a_chain_deeper_than_the_cap_is_refused(self, workspace):
        # Built straight through the ORM: create_segment validates, so the
        # service would refuse to build the over-deep chain in the first place —
        # which is the point, but it is not what this test is checking.
        current = Segment.objects.create(workspace=workspace, name="s0", filter_json={"match": "all", "rules": []})
        for index in range(1, 6):
            current = Segment.objects.create(
                workspace=workspace,
                name=f"s{index}",
                filter_json={"match": "all", "rules": [{"source": "segment", "key": str(current.pk), "op": "in"}]},
            )

        with pytest.raises(ConditionValidationError) as exc:
            validate(workspace, {"match": "all", "rules": [{"source": "segment", "key": str(current.pk), "op": "in"}]})

        assert exc.value.code in {"segment_too_deep", "budget_exceeded"}

    def test_a_wide_tree_cannot_expand_past_the_total_rule_budget(self, workspace):
        tag, _ = services.get_or_create_tag(workspace, "vip")
        wide = {"match": "any", "rules": [{"source": "tag", "key": str(tag.pk), "op": "has"}] * MAX_RULES}
        leaf = services.create_segment(workspace, name="leaf", filter_json=wide)
        middle = Segment.objects.create(
            workspace=workspace,
            name="middle",
            filter_json={"match": "any", "rules": [{"source": "segment", "key": str(leaf.pk), "op": "in"}] * 10},
        )

        with pytest.raises(ConditionValidationError) as exc:
            validate(workspace, {"match": "any", "rules": [{"source": "segment", "key": str(middle.pk), "op": "in"}]})

        assert exc.value.code == "budget_exceeded"


@pytest.mark.django_db
class TestValuesThatAreLegalButSharp:
    @pytest.mark.parametrize("needle", ["%", "_", "\\", "100%", "a%b_c"])
    def test_like_metacharacters_match_literally(self, workspace, needle):
        """Parameter binding does not solve this one: a regression here would
        turn `contains "%"` into `matches everyone`."""
        services.create_contact(workspace, first_name="Plain", email="plain@example.test")
        marked = services.create_contact(workspace, first_name="Marked", email=f"a{needle}b@example.test")

        got = queryset(
            workspace,
            {"match": "all", "rules": [{"source": "system_field", "key": "email", "op": "contains", "value": needle}]},
        )

        assert {c.pk for c in got} == {marked.pk}

    def test_a_sql_shaped_value_matches_nothing_and_breaks_nothing(self, workspace):
        services.create_contact(workspace, email="real@example.test")
        payload = {
            "match": "all",
            "rules": [
                {"source": "system_field", "key": "email", "op": "is", "value": "x'; DROP TABLE contacts_contact; --"}
            ],
        }

        assert list(queryset(workspace, payload)) == []


@pytest.mark.django_db
class TestEveryRefusalIsAConditionError:
    """A bad document must never escape as something a caller does not catch.

    ``Segment.clean()`` catches ``ConditionValidationError`` and
    ``views.contact_list`` catches ``ConditionError``; anything else reaching
    them is a 500 for input a stranger supplied. The corpus above already
    asserts the type for every payload, so this covers only the one case where
    getting the type right and getting the *code* right pull against each other.
    """

    def test_a_rejected_constant_keeps_its_own_error_code(self, workspace):
        """ConditionValidationError is itself a ValueError, so the broadened
        except clause in _load must re-raise it rather than relabel it."""
        payload = '{"match":"all","rules":[{"source":"system_field","key":"email","op":"is","value":NaN}]}'

        with pytest.raises(ConditionValidationError) as exc:
            validate(workspace, payload)

        assert exc.value.code == "bad_number"


@pytest.mark.django_db
class TestTheSizeCapAppliesToParsedDocumentsToo:
    """The byte cap used to run only on the raw-text path.

    Every real caller hands over a parsed dict — Django's JSONField, the admin
    form, and any JSON API — so the identical document sailed past the limit it
    was written to enforce (SECURITY-BASELINE §7).
    """

    @staticmethod
    def _fat_document(rules: int) -> dict:
        return {
            "match": "any",
            "rules": [
                {"source": "system_field", "key": "email", "op": "contains", "value": "x" * MAX_VALUE_CHARS}
                for _ in range(rules)
            ],
        }

    def test_an_oversized_dict_is_refused(self, workspace):
        document = self._fat_document(MAX_RULES)

        with pytest.raises(ConditionValidationError) as exc:
            validate(workspace, document)

        assert exc.value.code == "too_large"

    def test_the_same_document_as_text_is_refused_identically(self, workspace):
        """The two paths must agree; they used to disagree by 12 KiB."""
        import json

        document = self._fat_document(MAX_RULES)

        with pytest.raises(ConditionValidationError) as as_dict:
            validate(workspace, document)
        with pytest.raises(ConditionValidationError) as as_text:
            validate(workspace, json.dumps(document))

        assert as_dict.value.code == as_text.value.code == "too_large"

    def test_a_document_within_the_cap_still_validates(self, workspace):
        """The cap must not have been tightened into the legal range."""
        validate(workspace, self._fat_document(4))
