"""ROADMAP contract 8: the operator matrix, in both evaluation modes.

Every case is asserted three ways — against an expected set, for agreement
between ``queryset()`` and ``evaluate()``, and (for operators that come in
pairs) for exactly partitioning the workspace. The agreement test is the one
that matters most: a disagreement between the two modes is a contact receiving a
message their segment says they will not receive.
"""

import copy
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from django.db.models import Exists, OuterRef, Q

from apps.contacts import services
from apps.contacts.conditions import (
    ALL_OPS,
    CONDITION_SCHEMA,
    NEGATION_PAIRS,
    OPS_BY_SOURCE,
    SOURCE_NAMES,
    SYSTEM_FIELDS,
    ConditionSource,
    ConditionValidationError,
    SourceContractError,
    SourceNotEvaluableError,
    evaluate,
    evaluate_many,
    queryset,
    register_source,
    sources,
)
from apps.contacts.models import Contact, ContactStatus, ContactTag, CustomFieldType

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
REF_DAY = "2026-08-21"

# Hoisted out of the rule literal below on purpose. A UUID sitting directly
# after `"key":` reads to gitleaks' generic-api-key rule as a keyword next to a
# high-entropy value, and the scan runs over full history so an inline
# `# gitleaks:allow` would only cover the commit that carries it. Every later
# workstream writing a condition or graph fixture will meet the same trap.
A_SEQUENCE_ID = "8f14e45f-ceea-467a-9f52-9d9c9d9c9d9c"


@dataclass
class World:
    workspace: Any
    contacts: dict[str, Contact] = dataclass_field(default_factory=dict)
    tags: dict[str, Any] = dataclass_field(default_factory=dict)
    fields: dict[str, Any] = dataclass_field(default_factory=dict)
    segments: dict[str, Any] = dataclass_field(default_factory=dict)

    @property
    def active(self) -> dict[str, Contact]:
        return {h: c for h, c in self.contacts.items() if c.status == ContactStatus.ACTIVE}

    def pks(self, handles) -> set:
        return {self.contacts[h].pk for h in handles}


@pytest.fixture
def world(db, tenancy):
    """One fixture world covering every edge the matrix needs.

    Includes contacts with no tag row and no field row at all, because "does a
    negative operator match an absent row?" is the question the whole compiler
    turns on.
    """
    ws = tenancy.workspace
    w = World(workspace=ws)

    w.tags["vip"] = services.get_or_create_tag(ws, "VIP")[0]
    w.fields["city"] = services.create_custom_field(ws, name="City", field_type=CustomFieldType.TEXT)
    w.fields["plan"] = services.create_custom_field(ws, name="Plan", field_type=CustomFieldType.NUMBER)
    w.fields["signup"] = services.create_custom_field(ws, name="Signup", field_type=CustomFieldType.DATE)
    w.fields["seen"] = services.create_custom_field(ws, name="Seen", field_type=CustomFieldType.DATETIME)
    w.fields["pro"] = services.create_custom_field(ws, name="Pro", field_type=CustomFieldType.BOOLEAN)

    def make(handle: str, **kwargs) -> Contact:
        w.contacts[handle] = services.create_contact(ws, **kwargs)
        return w.contacts[handle]

    make("bare")
    make("vip", email="vip@example.test")
    make("paris")
    make("empty_city")
    make("plan10")
    make("plan20")
    make("pro_yes")
    make("pro_no")
    make("early", last_interaction_at=NOW - timedelta(days=1))
    make("today", last_interaction_at=NOW)
    make("late", last_interaction_at=NOW + timedelta(days=1))
    make("gone")

    services.add_tag(w.contacts["vip"], w.tags["vip"])
    services.set_field_value(w.contacts["paris"], w.fields["city"], "Paris")
    services.set_field_value(w.contacts["empty_city"], w.fields["city"], "")
    services.set_field_value(w.contacts["plan10"], w.fields["plan"], 10)
    services.set_field_value(w.contacts["plan20"], w.fields["plan"], 20)
    services.set_field_value(w.contacts["pro_yes"], w.fields["pro"], True)
    services.set_field_value(w.contacts["pro_no"], w.fields["pro"], False)
    for handle, days in (("early", -1), ("today", 0), ("late", 1)):
        day = (NOW + timedelta(days=days)).date()
        services.set_field_value(w.contacts[handle], w.fields["signup"], day)
        services.set_field_value(w.contacts[handle], w.fields["seen"], NOW + timedelta(days=days))

    w.contacts["gone"].status = ContactStatus.DELETED
    w.contacts["gone"].save(update_fields=["status"])

    w.segments["vips"] = services.create_segment(
        ws,
        name="VIPs",
        filter_json={"match": "all", "rules": [{"source": "tag", "key": str(w.tags["vip"].pk), "op": "has"}]},
    )
    return w


@dataclass(frozen=True)
class Case:
    id: str
    op: str
    rule: dict[str, Any]
    expect: frozenset[str]


def _cases(w: World) -> tuple[Case, ...]:
    tag_id = str(w.tags["vip"].pk)
    city, plan, signup, seen, pro = (str(w.fields[n].pk) for n in ("city", "plan", "signup", "seen", "pro"))
    segment_id = str(w.segments["vips"].pk)
    everyone = frozenset(w.active)

    def case(name, op, rule, expect) -> Case:
        return Case(id=name, op=op, rule=rule, expect=frozenset(expect))

    return (
        # -- tag ---------------------------------------------------------
        case("tag.has", "has", {"source": "tag", "key": tag_id, "op": "has"}, {"vip"}),
        case("tag.has_not", "has_not", {"source": "tag", "key": tag_id, "op": "has_not"}, everyone - {"vip"}),
        # -- segment -----------------------------------------------------
        case("segment.in", "in", {"source": "segment", "key": segment_id, "op": "in"}, {"vip"}),
        case("segment.not_in", "not_in", {"source": "segment", "key": segment_id, "op": "not_in"}, everyone - {"vip"}),
        # -- custom_field, text -----------------------------------------
        case("cf.text.is", "is", {"source": "custom_field", "key": city, "op": "is", "value": "paris"}, {"paris"}),
        case(
            "cf.text.is_not",
            "is_not",
            {"source": "custom_field", "key": city, "op": "is_not", "value": "paris"},
            everyone - {"paris"},
        ),
        case(
            "cf.text.contains",
            "contains",
            {"source": "custom_field", "key": city, "op": "contains", "value": "ari"},
            {"paris"},
        ),
        case("cf.text.has_value", "has_value", {"source": "custom_field", "key": city, "op": "has_value"}, {"paris"}),
        case(
            "cf.text.no_value",
            "no_value",
            {"source": "custom_field", "key": city, "op": "no_value"},
            everyone - {"paris"},
        ),
        # -- custom_field, number ---------------------------------------
        case("cf.num.eq", "=", {"source": "custom_field", "key": plan, "op": "=", "value": 10}, {"plan10"}),
        case(
            "cf.num.ne", "!=", {"source": "custom_field", "key": plan, "op": "!=", "value": 10}, everyone - {"plan10"}
        ),
        case("cf.num.gt", ">", {"source": "custom_field", "key": plan, "op": ">", "value": 10}, {"plan20"}),
        case("cf.num.lt", "<", {"source": "custom_field", "key": plan, "op": "<", "value": 20}, {"plan10"}),
        case(
            "cf.num.gte", ">=", {"source": "custom_field", "key": plan, "op": ">=", "value": 10}, {"plan10", "plan20"}
        ),
        case(
            "cf.num.lte", "<=", {"source": "custom_field", "key": plan, "op": "<=", "value": 20}, {"plan10", "plan20"}
        ),
        # -- custom_field, date ------------------------------------------
        case(
            "cf.date.before",
            "before",
            {"source": "custom_field", "key": signup, "op": "before", "value": REF_DAY},
            {"early"},
        ),
        case("cf.date.on", "on", {"source": "custom_field", "key": signup, "op": "on", "value": REF_DAY}, {"today"}),
        case(
            "cf.date.after",
            "after",
            {"source": "custom_field", "key": signup, "op": "after", "value": REF_DAY},
            {"late"},
        ),
        # -- custom_field, datetime (day-granular, half-open ranges) -----
        case(
            "cf.dt.before",
            "before",
            {"source": "custom_field", "key": seen, "op": "before", "value": REF_DAY},
            {"early"},
        ),
        case("cf.dt.on", "on", {"source": "custom_field", "key": seen, "op": "on", "value": REF_DAY}, {"today"}),
        case(
            "cf.dt.after", "after", {"source": "custom_field", "key": seen, "op": "after", "value": REF_DAY}, {"late"}
        ),
        # -- custom_field, boolean ---------------------------------------
        case("cf.bool.is", "is", {"source": "custom_field", "key": pro, "op": "is", "value": True}, {"pro_yes"}),
        # -- system_field, text ------------------------------------------
        case(
            "sf.text.is",
            "is",
            {"source": "system_field", "key": "email", "op": "is", "value": "VIP@example.test"},
            {"vip"},
        ),
        case(
            "sf.text.contains",
            "contains",
            {"source": "system_field", "key": "email", "op": "contains", "value": "vip@"},
            {"vip"},
        ),
        case(
            "sf.text.has_value",
            "has_value",
            {"source": "system_field", "key": "email", "op": "has_value"},
            {"vip"},
        ),
        case(
            "sf.text.no_value",
            "no_value",
            {"source": "system_field", "key": "email", "op": "no_value"},
            everyone - {"vip"},
        ),
        case(
            "sf.text.is_not",
            "is_not",
            {"source": "system_field", "key": "email", "op": "is_not", "value": "vip@example.test"},
            everyone - {"vip"},
        ),
        # -- system_field, datetime (nullable column) --------------------
        case(
            "sf.dt.before",
            "before",
            {"source": "system_field", "key": "last_interaction_at", "op": "before", "value": REF_DAY},
            {"early"},
        ),
        case(
            "sf.dt.on",
            "on",
            {"source": "system_field", "key": "last_interaction_at", "op": "on", "value": REF_DAY},
            {"today"},
        ),
        case(
            "sf.dt.after",
            "after",
            {"source": "system_field", "key": "last_interaction_at", "op": "after", "value": REF_DAY},
            {"late"},
        ),
    )


def _filter(rule: dict[str, Any], match: str = "all") -> dict[str, Any]:
    return {"match": match, "rules": [rule]}


@pytest.mark.django_db
class TestTheOperatorMatrix:
    def test_set_wise_evaluation_returns_the_expected_contacts(self, world):
        for case in _cases(world):
            got = {c.pk for c in queryset(world.workspace, _filter(case.rule), now=NOW)}
            assert got == world.pks(case.expect), case.id

    def test_row_wise_evaluation_returns_the_expected_contacts(self, world):
        for case in _cases(world):
            got = {c.pk for h, c in world.active.items() if evaluate(c, _filter(case.rule), now=NOW)}
            assert got == world.pks(case.expect), case.id

    def test_the_two_modes_agree(self, world):
        """Independent of the expectations above: proves equivalence even where
        a Case.expect is itself wrong."""
        for case in _cases(world):
            set_wise = {c.pk for c in queryset(world.workspace, _filter(case.rule), now=NOW)}
            row_wise = {c.pk for c in world.active.values() if evaluate(c, _filter(case.rule), now=NOW)}
            assert set_wise == row_wise, case.id

    def test_every_operator_the_engine_declares_is_exercised(self, world):
        """Structural coverage, in the spirit of the IDOR sweep: an operator with
        no case turns this red rather than being quietly untested."""
        slots = set(OPS_BY_SOURCE["sequence"]) | set(OPS_BY_SOURCE["window"])
        covered = {case.op for case in _cases(world)}

        assert (ALL_OPS - slots) <= covered

    def test_a_soft_deleted_contact_is_never_in_the_set(self, world):
        """status is a tombstone marker, not a segmentation dimension: a filter
        that could target deleted people would put them back in a send path."""
        for case in _cases(world):
            assert "gone" not in case.expect
        everyone = queryset(world.workspace, {"match": "all", "rules": []}, now=NOW)
        assert world.contacts["gone"].pk not in {c.pk for c in everyone}


@pytest.mark.django_db
class TestNegativesIncludeAbsence:
    """ "Everyone not tagged VIP" must include the contacts never tagged at all.

    That is also what makes each pair an exact partition, which is asserted
    numerically off NEGATION_PAIRS so a new pair cannot ship half-tested.
    """

    @staticmethod
    def _pair_rules(world: World, positive: str, negative: str) -> tuple[dict, dict] | None:
        city = str(world.fields["city"].pk)
        plan = str(world.fields["plan"].pk)
        by_positive: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {
            "is": ({"source": "custom_field", "key": city, "op": "is", "value": "Paris"}, {"op": "is_not"}),
            "=": ({"source": "custom_field", "key": plan, "op": "=", "value": 10}, {"op": "!="}),
            "has_value": ({"source": "custom_field", "key": city, "op": "has_value"}, {"op": "no_value"}),
            "has": ({"source": "tag", "key": str(world.tags["vip"].pk), "op": "has"}, {"op": "has_not"}),
            "in": ({"source": "segment", "key": str(world.segments["vips"].pk), "op": "in"}, {"op": "not_in"}),
        }
        if positive not in by_positive:
            return None
        rule, override = by_positive[positive]
        return rule, {**rule, **override}

    @pytest.mark.parametrize(("positive", "negative"), NEGATION_PAIRS, ids=lambda v: str(v))
    def test_a_negative_operator_is_the_exact_complement(self, world, positive, negative):
        rules = self._pair_rules(world, positive, negative)
        if rules is None:
            pytest.skip(f"{positive}/{negative} belongs to a source no deployment implements yet")
        positive_rule, negative_rule = rules

        yes = {c.pk for c in queryset(world.workspace, _filter(positive_rule), now=NOW)}
        no = {c.pk for c in queryset(world.workspace, _filter(negative_rule), now=NOW)}

        assert yes & no == set()
        assert yes | no == {c.pk for c in world.active.values()}


@pytest.mark.django_db
class TestMatchModes:
    def test_all_intersects_and_any_unions(self, world):
        rules = [
            {"source": "custom_field", "key": str(world.fields["plan"].pk), "op": ">=", "value": 10},
            {"source": "tag", "key": str(world.tags["vip"].pk), "op": "has"},
        ]

        both = queryset(world.workspace, {"match": "all", "rules": rules}, now=NOW)
        either = queryset(world.workspace, {"match": "any", "rules": rules}, now=NOW)

        assert {c.pk for c in both} == set()
        assert {c.pk for c in either} == world.pks({"plan10", "plan20", "vip"})

    def test_no_rules_under_all_matches_everyone(self, world):
        """The identity of AND — and a live hazard: an empty segment handed to a
        broadcast targets the whole workspace, which is why issue #23 must show
        a count before sending."""
        got = queryset(world.workspace, {"match": "all", "rules": []}, now=NOW)

        assert {c.pk for c in got} == {c.pk for c in world.active.values()}

    def test_no_rules_under_any_matches_nobody(self, world):
        got = queryset(world.workspace, {"match": "any", "rules": []}, now=NOW)

        assert list(got) == []

    def test_three_rules_nest_correctly_under_any(self, world):
        rules = [
            {"source": "custom_field", "key": str(world.fields["city"].pk), "op": "is", "value": "Paris"},
            {"source": "custom_field", "key": str(world.fields["pro"].pk), "op": "is", "value": True},
            {"source": "tag", "key": str(world.tags["vip"].pk), "op": "has"},
        ]

        got = queryset(world.workspace, {"match": "any", "rules": rules}, now=NOW)

        assert {c.pk for c in got} == world.pks({"paris", "pro_yes", "vip"})


@pytest.mark.django_db
class TestRelativeDates:
    @pytest.mark.parametrize(
        ("offset", "op", "expected"),
        [
            (0, "on", {"today"}),
            (-1, "on", {"early"}),
            (1, "on", {"late"}),
            (0, "before", {"early"}),
            (0, "after", {"late"}),
        ],
    )
    def test_an_offset_resolves_against_now(self, world, offset, op, expected):
        rule = {
            "source": "system_field",
            "key": "last_interaction_at",
            "op": op,
            "value": {"relative": {"unit": "days", "offset": offset}},
        }

        got = queryset(world.workspace, _filter(rule), now=NOW)

        assert {c.pk for c in got} == world.pks(expected)

    def test_before_on_and_after_partition_the_non_null_rows(self, world):
        def matching(op):
            rule = {"source": "system_field", "key": "last_interaction_at", "op": op, "value": REF_DAY}
            return {c.pk for c in queryset(world.workspace, _filter(rule), now=NOW)}

        assert matching("before") | matching("on") | matching("after") == world.pks({"early", "today", "late"})
        assert matching("before") & matching("on") == set()
        assert matching("on") & matching("after") == set()

    def test_day_boundaries_follow_the_workspace_timezone(self, world):
        """One timezone per query, taken from Workspace.effective_timezone. SPEC
        §11.5 gives smart_delay a use_contact_timezone flag; §11.4 pointedly
        does not, and a per-contact boundary could not be a single statement."""
        moment = datetime(2026, 8, 21, 13, 0, tzinfo=UTC)  # 2026-08-22 01:00 in Auckland
        world.contacts["today"].last_interaction_at = moment
        world.contacts["today"].save(update_fields=["last_interaction_at"])
        rule = {"source": "system_field", "key": "last_interaction_at", "op": "on", "value": "2026-08-22"}

        in_utc = {c.pk for c in queryset(world.workspace, _filter(rule), now=NOW)}

        world.workspace.timezone = "Pacific/Auckland"
        world.workspace.save(update_fields=["timezone"])
        in_auckland = {c.pk for c in queryset(world.workspace, _filter(rule), now=NOW)}

        assert world.contacts["today"].pk not in in_utc
        assert world.contacts["today"].pk in in_auckland


@pytest.mark.django_db
class TestSegments:
    def test_a_segment_inlines_rather_than_becoming_a_subquery(self, world):
        """Nesting is expressed through the segment source (SPEC §11.4's schema
        is flat), and the nested group is compiled into the same WHERE clause,
        so the whole filter stays one statement."""
        rule = {"source": "segment", "key": str(world.segments["vips"].pk), "op": "in"}

        sql = str(queryset(world.workspace, _filter(rule), now=NOW).query)

        assert sql.count("EXISTS") == 1  # the tag rule's, not a segment subquery

    def test_segments_nest_to_the_declared_depth(self, world):
        inner = world.segments["vips"]
        middle = services.create_segment(
            world.workspace,
            name="Middle",
            filter_json={"match": "all", "rules": [{"source": "segment", "key": str(inner.pk), "op": "in"}]},
        )
        outer = services.create_segment(
            world.workspace,
            name="Outer",
            filter_json={"match": "all", "rules": [{"source": "segment", "key": str(middle.pk), "op": "in"}]},
        )

        got = queryset(world.workspace, _filter({"source": "segment", "key": str(outer.pk), "op": "in"}), now=NOW)

        assert {c.pk for c in got} == world.pks({"vip"})

    def test_a_cycle_is_a_validation_error_not_a_recursion_error(self, world):
        first = world.segments["vips"]
        second = services.create_segment(
            world.workspace,
            name="Second",
            filter_json={"match": "all", "rules": [{"source": "segment", "key": str(first.pk), "op": "in"}]},
        )
        first.filter_json = {"match": "all", "rules": [{"source": "segment", "key": str(second.pk), "op": "in"}]}
        first.save(update_fields=["filter_json"])

        with pytest.raises(ConditionValidationError) as exc:
            queryset(world.workspace, _filter({"source": "segment", "key": str(first.pk), "op": "in"}))

        assert exc.value.code == "segment_cycle"

    def test_a_segment_cannot_reference_itself(self, world):
        from apps.contacts.conditions import validate

        segment = world.segments["vips"]
        with pytest.raises(ConditionValidationError) as exc:
            validate(
                world.workspace,
                _filter({"source": "segment", "key": str(segment.pk), "op": "in"}),
                exclude_segment_id=segment.pk,
            )

        assert exc.value.code == "segment_cycle"

    def test_another_workspaces_segment_is_unknown_not_forbidden(self, world, other_tenancy):
        """No existence oracle: the message must not distinguish "someone else's"
        from "no such thing"."""
        theirs = services.create_segment(
            other_tenancy.workspace, name="Theirs", filter_json={"match": "all", "rules": []}
        )

        with pytest.raises(ConditionValidationError) as exc:
            queryset(world.workspace, _filter({"source": "segment", "key": str(theirs.pk), "op": "in"}))

        assert exc.value.code == "unknown_segment"


@pytest.mark.django_db
class TestUnimplementedSources:
    @pytest.mark.parametrize(
        ("rule", "owner"),
        [
            ({"source": "window", "key": "telegram", "op": "inside"}, "#8"),
            ({"source": "sequence", "key": A_SEQUENCE_ID, "op": "subscribed"}, "#22"),
        ],
    )
    def test_a_slot_validates_but_refuses_to_evaluate(self, world, rule, owner):
        """Issue #6 can ship the whole builder panel before #8 and #22 exist, so
        a filter using a slot must be saveable — and must fail clearly if run."""
        from apps.contacts.conditions import validate

        validate(world.workspace, _filter(rule))

        with pytest.raises(SourceNotEvaluableError) as exc:
            list(queryset(world.workspace, _filter(rule)))

        assert owner in str(exc.value)


@pytest.mark.django_db
class TestBatchEvaluation:
    def test_evaluate_many_answers_for_a_whole_batch(self, world):
        rule = {"source": "tag", "key": str(world.tags["vip"].pk), "op": "has"}

        got = evaluate_many(world.workspace, world.active.values(), _filter(rule), now=NOW)

        assert got == world.pks({"vip"})

    def test_an_empty_batch_costs_no_query(self, world, django_assert_num_queries):
        with django_assert_num_queries(0):
            assert evaluate_many(world.workspace, [], {"match": "all", "rules": []}) == set()


class TestTheRegistry:
    def test_all_six_sources_are_declared(self):
        assert tuple(sources()) == SOURCE_NAMES

    def test_the_two_slots_are_declared_but_not_evaluable(self):
        assert sources()["window"].is_evaluable is False
        assert sources()["sequence"].is_evaluable is False
        assert all(sources()[name].is_evaluable for name in ("tag", "custom_field", "system_field", "segment"))

    def test_registering_an_undeclared_source_is_refused(self):
        with pytest.raises(SourceContractError):
            register_source(ConditionSource("astrology", "Astrology", "uuid", ("is",), lambda ctx, rule: Q()))

    def test_a_registration_may_not_change_the_operator_vocabulary(self):
        with pytest.raises(SourceContractError):
            register_source(
                ConditionSource("window", "Messaging window", "platform", ("open", "shut"), lambda ctx, rule: Q())
            )

    def test_registering_the_identical_declaration_twice_is_a_no_op(self):
        """AppConfig.ready() runs twice under some autoreload paths."""
        register_source(sources()["window"])

        assert sources()["window"].is_evaluable is False

    def test_registering_an_implementation_then_restoring_it(self):
        original = sources()["window"]
        implemented = ConditionSource(
            original.name, original.label, original.key_kind, original.ops, lambda ctx, rule: Q(), original.owner
        )
        try:
            register_source(implemented)
            assert sources()["window"].is_evaluable is True
            # A second, different implementation needs an explicit replace=True.
            second = ConditionSource(
                original.name, original.label, original.key_kind, original.ops, lambda ctx, rule: ~Q(), original.owner
            )
            with pytest.raises(SourceContractError):
                register_source(second)
        finally:
            register_source(original, replace=True)
        assert sources()["window"].is_evaluable is False


class TestTheSchema:
    def test_it_round_trips_as_plain_json(self):
        """Issue #6 embeds this dict and a React builder generates panels from
        it, so a tuple or an enum member leaking into a leaf breaks their build,
        not ours."""
        import json

        assert json.loads(json.dumps(CONDITION_SCHEMA)) == CONDITION_SCHEMA

    def test_every_variants_operator_enum_matches_the_python_tables(self):
        from apps.contacts.conditions import TYPE_OPS, _legal_ops

        for variant in CONDITION_SCHEMA["properties"]["rules"]["items"]["oneOf"]:
            source = variant["properties"]["source"]["const"]
            assert set(variant["properties"]["op"]["enum"]) == set(_legal_ops(source)), source
        assert set(CONDITION_SCHEMA["x-brightbean"]["opsByType"]) == {
            t for t in CONDITION_SCHEMA["x-brightbean"]["opsByType"]
        }
        assert TYPE_OPS

    def test_it_declares_all_six_sources_including_the_unimplemented_ones(self):
        declared = [
            v["properties"]["source"]["const"] for v in CONDITION_SCHEMA["properties"]["rules"]["items"]["oneOf"]
        ]

        assert declared == list(SOURCE_NAMES)
        assert CONDITION_SCHEMA["x-brightbean"]["unimplementedSources"] == ["sequence", "window"]

    def test_the_system_field_key_enum_is_the_allowlist(self):
        variant = next(
            v
            for v in CONDITION_SCHEMA["properties"]["rules"]["items"]["oneOf"]
            if v["properties"]["source"]["const"] == "system_field"
        )

        assert variant["properties"]["key"]["enum"] == sorted(SYSTEM_FIELDS)
        assert "status" not in SYSTEM_FIELDS

    def test_it_does_not_change_when_a_source_gains_an_implementation(self):
        """Built from the frozen vocabulary, not from the registry — otherwise
        the schema issue #6 embeds would depend on which apps had imported."""
        before = copy.deepcopy(CONDITION_SCHEMA)
        original = sources()["window"]
        implemented = ConditionSource(
            original.name, original.label, original.key_kind, original.ops, lambda ctx, rule: Q(), original.owner
        )
        try:
            register_source(implemented)
            assert before == CONDITION_SCHEMA
        finally:
            register_source(original, replace=True)

    def test_every_rule_variant_forbids_unknown_keys(self):
        for variant in CONDITION_SCHEMA["properties"]["rules"]["items"]["oneOf"]:
            assert variant["additionalProperties"] is False
        assert CONDITION_SCHEMA["additionalProperties"] is False


@pytest.mark.django_db
class TestSubqueriesCarryTheirOwnTenancy:
    def test_exists_does_not_trip_the_scoping_guard(self, world):
        """Documents the hazard as an executable fact.

        A queryset handed to Exists() is compiled, never executed, so
        WorkspaceScopedQuerySet's guard never fires on it. That is why every
        subquery in conditions.py is built with .for_workspace() rather than
        relying on the guard.
        """
        unscoped = ContactTag.objects.filter(contact=OuterRef("pk"))

        # No UnscopedQueryError, on purpose.
        Contact.objects.for_workspace(world.workspace).filter(Q(Exists(unscoped))).count()

    def test_the_compiled_subquery_filters_on_workspace(self, world):
        rule = {"source": "tag", "key": str(world.tags["vip"].pk), "op": "has"}

        sql = str(queryset(world.workspace, _filter(rule), now=NOW).query)
        inside_exists = sql.split("EXISTS", 1)[1]

        assert "workspace_id" in inside_exists

    def test_another_workspaces_contacts_are_never_returned(self, world, other_tenancy):
        services.create_contact(other_tenancy.workspace, first_name="Eve")

        got = queryset(world.workspace, {"match": "all", "rules": []}, now=NOW)

        assert {c.workspace_id for c in got} == {world.workspace.pk}

    def test_a_hostile_value_is_bound_as_a_parameter_not_spliced_into_sql(self, world):
        hostile = "vip'); DROP TABLE contacts_contact; --"
        rule = {"source": "system_field", "key": "email", "op": "contains", "value": hostile}

        compiled = queryset(world.workspace, _filter(rule), now=NOW).query.get_compiler("default")
        sql, params = compiled.as_sql()

        # The statement never carries it; only the parameter list does.
        assert "DROP TABLE" not in sql
        assert any("DROP TABLE" in str(item) for item in params)

        # And running it leaves the table standing.
        assert list(queryset(world.workspace, _filter(rule), now=NOW)) == []
        assert Contact.objects.for_workspace(world.workspace).count() == len(world.contacts)


@pytest.mark.django_db
class TestTheConstantPredicates:
    """An empty group is a real predicate, not Django's identity element.

    A bare ``Q()`` vanishes in two directions: ``~Q()`` compiles to no predicate
    at all, so ``not_in`` a segment matching everyone silently dropped the
    exclusion; and ``Q() | other`` collapses to ``other``, so an ``any`` group
    containing such a segment under-matched. Both are asserted here because both
    were live.
    """

    @pytest.fixture
    def everyone_segment(self, world):
        return services.create_segment(world.workspace, name="Everyone", filter_json={"match": "all", "rules": []})

    def test_in_a_segment_that_matches_everyone_matches_everyone(self, world, everyone_segment):
        rule = {"source": "segment", "key": str(everyone_segment.pk), "op": "in"}

        got = queryset(world.workspace, _filter(rule), now=NOW)

        assert {c.pk for c in got} == {c.pk for c in world.active.values()}

    def test_not_in_a_segment_that_matches_everyone_matches_nobody(self, world, everyone_segment):
        rule = {"source": "segment", "key": str(everyone_segment.pk), "op": "not_in"}

        got = queryset(world.workspace, _filter(rule), now=NOW)

        assert list(got) == []

    def test_the_exclusion_survives_being_anded_with_another_rule(self, world, everyone_segment):
        """The shape that made this a fail-open: the dropped clause is invisible
        because the surviving rules still return a plausible-looking set."""
        rules = [
            {"source": "segment", "key": str(everyone_segment.pk), "op": "not_in"},
            {"source": "tag", "key": str(world.tags["vip"].pk), "op": "has"},
        ]

        got = queryset(world.workspace, {"match": "all", "rules": rules}, now=NOW)

        assert list(got) == []

    def test_an_everyone_segment_still_dominates_an_any_group(self, world, everyone_segment):
        """`Q() | other` collapsed to `other`, so this under-matched."""
        rules = [
            {"source": "segment", "key": str(everyone_segment.pk), "op": "in"},
            {"source": "tag", "key": str(world.tags["vip"].pk), "op": "has"},
        ]

        got = queryset(world.workspace, {"match": "any", "rules": rules}, now=NOW)

        assert {c.pk for c in got} == {c.pk for c in world.active.values()}

    def test_a_segment_wrapping_an_empty_one_negates_correctly_too(self, world, everyone_segment):
        """One level deeper: the wrapper compiles to the same constant."""
        wrapper = services.create_segment(
            world.workspace,
            name="Wrapper",
            filter_json={
                "match": "all",
                "rules": [{"source": "segment", "key": str(everyone_segment.pk), "op": "in"}],
            },
        )
        rule = {"source": "segment", "key": str(wrapper.pk), "op": "not_in"}

        assert list(queryset(world.workspace, _filter(rule), now=NOW)) == []

    def test_in_and_not_in_partition_the_workspace_for_an_empty_segment(self, world, everyone_segment):
        """The invariant the module docstring claims, at the value that broke it."""
        key = str(everyone_segment.pk)
        inside = {c.pk for c in queryset(world.workspace, _filter({"source": "segment", "key": key, "op": "in"}))}
        outside = {c.pk for c in queryset(world.workspace, _filter({"source": "segment", "key": key, "op": "not_in"}))}

        assert inside & outside == set()
        assert inside | outside == {c.pk for c in world.active.values()}

    def test_a_nobody_segment_negates_to_everybody(self, world):
        """The mirror case, which already worked — pinned so it stays working."""
        nobody = services.create_segment(world.workspace, name="Nobody", filter_json={"match": "any", "rules": []})
        rule = {"source": "segment", "key": str(nobody.pk), "op": "not_in"}

        got = queryset(world.workspace, _filter(rule), now=NOW)

        assert {c.pk for c in got} == {c.pk for c in world.active.values()}
