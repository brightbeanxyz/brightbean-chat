"""Set-wise evaluation over 10k contacts is one SQL statement.

The acceptance criterion is "executes as SQL (assert query count)", so what is
asserted is the count rather than a wall-clock number: timings vary with the
machine, query counts do not. An N+1 regression — evaluating rules in Python and
filtering row by row — would show up here as ten thousand queries rather than
one.
"""

from datetime import UTC, datetime, timedelta

import pytest

from apps.contacts import services
from apps.contacts.conditions import evaluate, queryset, validate
from apps.contacts.models import Contact, ContactStatus, ContactTag, CustomFieldType, CustomFieldValue

POPULATION = 10_000
TAGGED = 1_000
WITH_PLAN = 4_000
NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


@pytest.fixture
def crowd(db, tenancy):
    """10 000 contacts in one workspace.

    Function-scoped and inside the ordinary ``db`` fixture on purpose. A
    class-scoped fixture would have to write through ``django_db_blocker``,
    which **commits** — leaving rows visible to every later test in the session.
    Re-seeding costs about a second per test and buys back full isolation.

    ``all_objects`` rather than ``objects``: ``bulk_create`` never reaches a
    guarded terminal operation, so the enforcing manager would work too — the
    plain one says out loud that no scope check happens here. ``bulk_create``
    also bypasses ``ContactScopedModel.save()``, so ``workspace`` is set by hand
    on the join rows; its docstring names this as the one caller that must.
    """
    ws = tenancy.workspace

    Contact.all_objects.bulk_create(
        (
            Contact(
                workspace=ws,
                first_name=f"C{index}",
                email=f"c{index}@example.test",
                status=ContactStatus.ACTIVE,
                last_interaction_at=NOW - timedelta(days=index % 30),
            )
            for index in range(POPULATION)
        ),
        batch_size=2_000,
    )
    # Ids only. Materialising 10 000 model instances just to reference them from
    # the join rows is most of the cost of this fixture, and `contact_id=` needs
    # nothing more than the key.
    ids = list(Contact.objects.for_workspace(ws).order_by("email").values_list("pk", flat=True))

    tag, _ = services.get_or_create_tag(ws, "VIP")
    plan = services.create_custom_field(ws, name="Plan", field_type=CustomFieldType.NUMBER)

    ContactTag.all_objects.bulk_create(
        (ContactTag(workspace=ws, contact_id=pk, tag=tag) for pk in ids[:TAGGED]),
        batch_size=2_000,
    )
    CustomFieldValue.all_objects.bulk_create(
        (
            CustomFieldValue(workspace=ws, contact_id=pk, field=plan, value_number=index % 50)
            for index, pk in enumerate(ids[:WITH_PLAN])
        ),
        batch_size=2_000,
    )
    segment = services.create_segment(
        ws,
        name="VIPs",
        filter_json={"match": "all", "rules": [{"source": "tag", "key": str(tag.pk), "op": "has"}]},
    )
    return {
        "workspace": ws,
        "tag": tag,
        "plan": plan,
        "segment": segment,
        "sample": Contact.objects.for_workspace(ws).get(pk=ids[0]),
    }


@pytest.mark.django_db
class TestTenThousandContacts:
    @pytest.fixture(autouse=True)
    def _bind(self, crowd):
        self.crowd = crowd
        self.filter_json = {
            "match": "all",
            "rules": [
                {"source": "tag", "key": str(crowd["tag"].pk), "op": "has"},
                {"source": "custom_field", "key": str(crowd["plan"].pk), "op": ">=", "value": 25},
                {"source": "segment", "key": str(crowd["segment"].pk), "op": "in"},
                {"source": "system_field", "key": "email", "op": "contains", "value": "@example.test"},
            ],
        }

    def test_validation_costs_a_bounded_number_of_queries(self, django_assert_num_queries):
        """One batched lookup per source kind, whatever the rule count: tags,
        custom fields, segments — plus one for the segment's own tag rule."""
        with django_assert_num_queries(4):
            validate(self.crowd["workspace"], self.filter_json)

    def test_set_wise_evaluation_is_a_single_statement(self, django_assert_num_queries):
        compiled = validate(self.crowd["workspace"], self.filter_json)

        with django_assert_num_queries(1):
            ids = list(queryset(self.crowd["workspace"], compiled, now=NOW).values_list("pk", flat=True))

        assert 0 < len(ids) < POPULATION

    def test_counting_is_a_single_statement(self, django_assert_num_queries):
        compiled = validate(self.crowd["workspace"], self.filter_json)

        with django_assert_num_queries(1):
            total = queryset(self.crowd["workspace"], compiled, now=NOW).count()

        assert total == 500  # 1000 tagged, half of which have plan >= 25

    def test_row_wise_evaluation_is_a_single_statement(self, django_assert_num_queries):
        """evaluate() delegating to queryset() means no per-contact fan-out."""
        compiled = validate(self.crowd["workspace"], self.filter_json)

        with django_assert_num_queries(1):
            evaluate(self.crowd["sample"], compiled, now=NOW)

    def test_a_date_rule_looks_the_workspace_timezone_up_at_most_once(self, django_assert_num_queries):
        """Resolved lazily and once per compilation.

        Passed a loaded ``Workspace`` — what a view has — it costs nothing,
        because ``effective_timezone`` reads an organization already cached on
        the instance. Passed a bare id — what ``evaluate()`` does, via
        ``contact.workspace_id`` — it costs exactly one lookup.
        """
        date_filter = {
            "match": "all",
            "rules": [{"source": "system_field", "key": "last_interaction_at", "op": "before", "value": "2026-08-21"}],
        }
        workspace = self.crowd["workspace"]
        compiled = validate(workspace, date_filter)

        with django_assert_num_queries(1):
            queryset(workspace, compiled, now=NOW).count()

        with django_assert_num_queries(2):
            queryset(workspace.pk, compiled, now=NOW).count()

    def test_a_filter_with_no_date_rule_never_looks_the_timezone_up(self, django_assert_num_queries):
        tag_only = {"match": "all", "rules": [{"source": "tag", "key": str(self.crowd["tag"].pk), "op": "has"}]}
        compiled = validate(self.crowd["workspace"], tag_only)

        with django_assert_num_queries(1):
            assert queryset(self.crowd["workspace"], compiled, now=NOW).count() == TAGGED

    def test_the_two_modes_still_agree_at_this_size(self):
        compiled = validate(self.crowd["workspace"], self.filter_json)
        matching = set(queryset(self.crowd["workspace"], compiled, now=NOW).values_list("pk", flat=True))

        sample = list(Contact.objects.for_workspace(self.crowd["workspace"]).order_by("email")[:50])
        for contact in sample:
            assert evaluate(contact, compiled, now=NOW) is (contact.pk in matching)
