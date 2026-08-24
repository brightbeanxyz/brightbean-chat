"""The two action-node verbs (ROADMAP contract 5, SPEC §11.2).

The schemas shipped with L2-D and the runtime is this issue's. What matters is
that a *published flow* containing them actually enrolls somebody — the registry
tests in ``apps/flows`` assert the wiring, and these assert the behaviour through
the engine, which is where a signature disagreement would show up.
"""

import pytest

from apps.campaigns.models import EnrollmentStatus, SequenceEnrollment
from apps.campaigns.tests.support import contact_for, sequence_with
from apps.flows.engine import start_flow
from apps.flows.models import ExecutionStatus, StartedBy
from apps.flows.tests.support import graph, node, published_flow


def _flow_with(workspace, *steps, name="Verb flow"):
    return published_flow(workspace, graph([node("a", "action", {"actions": list(steps)})]), name=name)


def _enrollments(workspace):
    return SequenceEnrollment.objects.for_workspace(workspace)


@pytest.mark.django_db
class TestSubscribeSequenceVerb:
    def test_it_enrolls_the_contact(self, tenancy):
        sequence = sequence_with(tenancy.workspace, steps=2)
        flow = _flow_with(tenancy.workspace, {"verb": "subscribe_sequence", "sequence": str(sequence.pk)})
        contact = contact_for(tenancy.workspace)

        execution = start_flow(contact, flow, started_by=StartedBy.API)

        assert execution.status == ExecutionStatus.COMPLETED
        enrollment = _enrollments(tenancy.workspace).get()
        assert enrollment.contact_id == contact.pk
        assert enrollment.status == EnrollmentStatus.ACTIVE

    def test_a_foreign_sequence_id_is_refused_and_the_node_continues(self, tenancy, other_tenancy, caplog):
        """A graph is editable by anyone with ``edit_flows``, so a hand-edited id
        must not reach another tenant. SPEC §11.2's "always Continue" means the
        rest of the node still runs."""
        theirs = sequence_with(other_tenancy.workspace, steps=1)
        flow = _flow_with(
            tenancy.workspace,
            {"verb": "subscribe_sequence", "sequence": str(theirs.pk)},
            {"verb": "add_tag", "tag": "still-ran"},
        )
        contact = contact_for(tenancy.workspace)

        with caplog.at_level("WARNING"):
            execution = start_flow(contact, flow, started_by=StartedBy.API)

        assert execution.status == ExecutionStatus.COMPLETED
        assert not _enrollments(tenancy.workspace).exists()
        assert not _enrollments(other_tenancy.workspace).exists()
        assert {tag.name for tag in contact.tags.all()} == {"still-ran"}
        assert "No such sequence" in caplog.text

    def test_a_malformed_id_is_refused_rather_than_crashing_the_run(self, tenancy, caplog):
        flow = _flow_with(tenancy.workspace, {"verb": "subscribe_sequence", "sequence": "not-a-uuid"})
        contact = contact_for(tenancy.workspace)

        with caplog.at_level("WARNING"):
            execution = start_flow(contact, flow, started_by=StartedBy.API)

        assert execution.status == ExecutionStatus.COMPLETED
        assert "not a sequence id" in caplog.text

    def test_re_running_the_flow_restarts_the_sequence(self, tenancy):
        """The verb inherits SPEC §12's re-enrollment rule rather than inventing one."""
        sequence = sequence_with(tenancy.workspace, steps=2)
        flow = _flow_with(tenancy.workspace, {"verb": "subscribe_sequence", "sequence": str(sequence.pk)})
        contact = contact_for(tenancy.workspace)

        start_flow(contact, flow, started_by=StartedBy.API)
        start_flow(contact, flow, started_by=StartedBy.API)

        statuses = sorted(_enrollments(tenancy.workspace).values_list("status", flat=True))
        assert statuses == [EnrollmentStatus.ACTIVE, EnrollmentStatus.UNSUBSCRIBED]


@pytest.mark.django_db
class TestUnsubscribeSequenceVerb:
    def test_it_stops_the_enrollment(self, tenancy):
        from apps.campaigns import services

        sequence = sequence_with(tenancy.workspace, steps=2)
        contact = contact_for(tenancy.workspace)
        services.subscribe(sequence, contact)
        flow = _flow_with(tenancy.workspace, {"verb": "unsubscribe_sequence", "sequence": str(sequence.pk)})

        start_flow(contact, flow, started_by=StartedBy.API)

        assert _enrollments(tenancy.workspace).get().status == EnrollmentStatus.UNSUBSCRIBED

    def test_unsubscribing_somebody_who_is_not_enrolled_is_a_quiet_no_op(self, tenancy):
        sequence = sequence_with(tenancy.workspace, steps=1)
        flow = _flow_with(tenancy.workspace, {"verb": "unsubscribe_sequence", "sequence": str(sequence.pk)})
        contact = contact_for(tenancy.workspace)

        execution = start_flow(contact, flow, started_by=StartedBy.API)

        assert execution.status == ExecutionStatus.COMPLETED
        assert not _enrollments(tenancy.workspace).exists()


@pytest.mark.django_db
class TestThePicklist:
    def test_the_builder_dropdown_fills_itself_with_no_edit_to_apps_flows(self, tenancy):
        """The Layer-6 gate item, asserted directly: ``_sequences`` resolves the
        model through ``installed_model("campaigns", ...)``, so shipping the app
        under that label is the whole wiring."""
        from apps.flows.picklists import picklists

        sequence = sequence_with(tenancy.workspace, steps=1, name="Onboarding")

        rows = picklists(tenancy.workspace)["sequences"]

        assert rows == [{"id": str(sequence.pk), "label": "Onboarding"}]

    def test_it_shows_only_this_workspace_s_sequences(self, tenancy, other_tenancy):
        from apps.flows.picklists import picklists

        sequence_with(other_tenancy.workspace, steps=1, name="Theirs")

        assert picklists(tenancy.workspace)["sequences"] == []
