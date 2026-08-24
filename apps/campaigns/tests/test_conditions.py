"""The ``sequence`` condition source (ROADMAP contract 8, SPEC §11.4).

The slot ``apps/contacts/conditions.py`` declared with a ``None`` handler and the
note "issue #22, L6-A". Three properties matter and each has a failure mode the
others do not catch: it filters **set-wise in one query**, its two operators
**partition** the workspace, and its subquery carries **its own tenancy**.
"""

import pytest

from apps.campaigns import services
from apps.campaigns.models import EnrollmentStatus, SequenceEnrollment
from apps.campaigns.tests.support import contact_for, sequence_with
from apps.contacts.conditions import evaluate, queryset, sources


def _filter(sequence, op):
    return {"match": "all", "rules": [{"source": "sequence", "key": str(sequence.pk), "op": op}]}


@pytest.mark.django_db
class TestTheSequenceSource:
    def test_it_is_registered_by_the_campaigns_app(self):
        source = sources()["sequence"]

        assert source.is_evaluable is True
        assert source.owner == "issue #22, L6-A"
        assert source.build_q.__module__ == "apps.campaigns.conditions"

    def test_subscribed_matches_the_people_on_the_sequence(self, tenancy):
        sequence = sequence_with(tenancy.workspace, steps=1)
        enrolled = contact_for(tenancy.workspace, first_name="Enrolled")
        contact_for(tenancy.workspace, first_name="Bystander")
        services.subscribe(sequence, enrolled)

        got = list(queryset(tenancy.workspace, _filter(sequence, "subscribed")))

        assert got == [enrolled]

    def test_not_matches_everybody_else_including_people_with_no_row(self, tenancy):
        sequence = sequence_with(tenancy.workspace, steps=1)
        enrolled = contact_for(tenancy.workspace, first_name="Enrolled")
        bystander = contact_for(tenancy.workspace, first_name="Bystander")
        services.subscribe(sequence, enrolled)

        got = set(queryset(tenancy.workspace, _filter(sequence, "not")))

        assert got == {bystander}

    def test_the_pair_partitions_the_workspace(self, tenancy):
        """Each operator pair has to be an exact complement, or a campaign built
        from one half and a suppression list from the other disagree."""
        sequence = sequence_with(tenancy.workspace, steps=1)
        for index in range(5):
            contact = contact_for(tenancy.workspace, first_name=f"C{index}")
            if index % 2:
                services.subscribe(sequence, contact)

        subscribed = set(queryset(tenancy.workspace, _filter(sequence, "subscribed")))
        rest = set(queryset(tenancy.workspace, _filter(sequence, "not")))

        assert subscribed & rest == set()
        assert len(subscribed | rest) == 5

    def test_a_completed_enrollment_is_not_subscribed(self, tenancy):
        """History is not membership: "not subscribed to onboarding" must not
        exclude everybody who ever finished it."""
        sequence = sequence_with(tenancy.workspace, steps=1)
        contact = contact_for(tenancy.workspace)
        enrollment = services.subscribe(sequence, contact)
        SequenceEnrollment.objects.for_workspace(tenancy.workspace).filter(pk=enrollment.pk).update(
            status=EnrollmentStatus.COMPLETED
        )

        assert list(queryset(tenancy.workspace, _filter(sequence, "subscribed"))) == []
        assert list(queryset(tenancy.workspace, _filter(sequence, "not"))) == [contact]

    def test_an_unsubscribed_enrollment_is_not_subscribed(self, tenancy):
        sequence = sequence_with(tenancy.workspace, steps=2)
        contact = contact_for(tenancy.workspace)
        services.subscribe(sequence, contact)
        services.unsubscribe(sequence, contact)

        assert list(queryset(tenancy.workspace, _filter(sequence, "subscribed"))) == []

    def test_evaluate_agrees_with_the_queryset(self, tenancy):
        sequence = sequence_with(tenancy.workspace, steps=1)
        contact = contact_for(tenancy.workspace)
        services.subscribe(sequence, contact)

        assert evaluate(contact, _filter(sequence, "subscribed")) is True
        assert evaluate(contact, _filter(sequence, "not")) is False

    def test_it_costs_one_query(self, tenancy, django_assert_num_queries):
        """Set-wise through the ORM, never a Python loop over contacts."""
        sequence = sequence_with(tenancy.workspace, steps=1)
        for index in range(10):
            services.subscribe(sequence, contact_for(tenancy.workspace, first_name=f"C{index}"))

        compiled = _filter(sequence, "subscribed")
        with django_assert_num_queries(1):
            assert len(list(queryset(tenancy.workspace, compiled))) == 10


@pytest.mark.django_db
class TestTenancy:
    def test_the_subquery_carries_its_own_workspace_predicate(self, tenancy):
        """The scoping guard does not fire inside ``Exists()`` — it compiles
        rather than executes — so ``for_workspace`` in the subquery is the only
        tenancy check it has. Read off the SQL, because a test that only checked
        the results would still pass with the predicate deleted."""
        sequence = sequence_with(tenancy.workspace, steps=1)

        sql = str(queryset(tenancy.workspace, _filter(sequence, "subscribed")).query)

        assert sql.count("workspace_id") >= 2

    def test_another_workspace_s_enrollment_is_invisible(self, tenancy, other_tenancy):
        """Two sequences of the same name in two tenants; the id resolves in one."""
        mine = sequence_with(tenancy.workspace, steps=1)
        theirs = sequence_with(other_tenancy.workspace, steps=1)
        services.subscribe(theirs, contact_for(other_tenancy.workspace))
        contact_for(tenancy.workspace)

        assert list(queryset(tenancy.workspace, _filter(mine, "subscribed"))) == []

    def test_a_filter_naming_another_workspace_s_sequence_matches_nobody(self, tenancy, other_tenancy):
        """A foreign id validates and then matches nothing, rather than being
        refused: the engine does not resolve a sequence key at validation time,
        so the *subquery's* ``for_workspace`` is what isolates it. That is also
        the answer SECURITY-BASELINE §1 wants — an id that names nothing here is
        indistinguishable from one that names nothing anywhere."""
        from apps.contacts.conditions import validate

        theirs = sequence_with(other_tenancy.workspace, steps=1)
        services.subscribe(theirs, contact_for(other_tenancy.workspace))
        mine = contact_for(tenancy.workspace)

        validate(tenancy.workspace, _filter(theirs, "subscribed"))

        assert list(queryset(tenancy.workspace, _filter(theirs, "subscribed"))) == []
        assert list(queryset(tenancy.workspace, _filter(theirs, "not"))) == [mine]
