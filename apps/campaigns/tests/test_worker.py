"""Enrollment, advance, completion and unsubscribe — SPEC §12 end to end.

Every test here drives the real queue: ``subscribe()`` writes a
``ScheduledAction`` and :func:`apps.queueing.worker.process_action` runs it, so
what is asserted is what a worker would actually do rather than what the handler
does when called directly.
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.campaigns import services
from apps.campaigns.models import DelayUnit, EnrollmentStatus, SequenceEnrollment, SequenceStatus
from apps.campaigns.scheduling import idempotency_key_for
from apps.campaigns.tests.support import contact_for, runnable_flow, sequence_with
from apps.contacts import services as contact_services
from apps.flows.models import ExecutionStatus, FlowExecution, StartedBy
from apps.queueing.models import ActionStatus, ActionType, ScheduledAction
from apps.queueing.worker import process_action


def _due_step(workspace):
    return ScheduledAction.objects.for_workspace(workspace).filter(type=ActionType.SEQUENCE_STEP)


def _run_next_step(workspace):
    """Claim and run the one pending step row, however far in the future it is."""
    action = _due_step(workspace).filter(status=ActionStatus.PENDING).order_by("run_at").first()
    assert action is not None, "expected a queued sequence step"
    action.status = ActionStatus.RUNNING
    action.save(update_fields=["status"])
    return process_action(action)


def _tags(contact):
    return {tag.name for tag in contact.tags.all()}


@pytest.mark.django_db
class TestSubscribing:
    def test_it_enrolls_at_step_one_and_queues_it(self, tenancy):
        sequence = sequence_with(tenancy.workspace, steps=2)
        contact = contact_for(tenancy.workspace)

        enrollment = services.subscribe(sequence, contact)

        assert enrollment.status == EnrollmentStatus.ACTIVE
        assert enrollment.current_step == 1
        assert enrollment.next_run_at is not None
        queued = _due_step(tenancy.workspace).get()
        assert queued.payload == {"enrollment_id": str(enrollment.pk), "position": 1}
        assert queued.idempotency_key == idempotency_key_for(enrollment, 1)
        assert queued.contact_id == contact.pk

    def test_the_first_run_is_the_first_step_s_delay_from_now(self, tenancy):
        sequence = sequence_with(tenancy.workspace, steps=1, delay_value=2, delay_unit=DelayUnit.HOURS)
        before = timezone.now()

        enrollment = services.subscribe(sequence, contact_for(tenancy.workspace))

        assert before + timedelta(hours=2) <= enrollment.next_run_at <= timezone.now() + timedelta(hours=2)

    def test_a_sequence_with_no_steps_completes_immediately(self, tenancy):
        """Nothing to wait for. The event still fires: they really were subscribed."""
        sequence = sequence_with(tenancy.workspace, steps=0)

        enrollment = services.subscribe(sequence, contact_for(tenancy.workspace))

        assert enrollment.status == EnrollmentStatus.COMPLETED
        assert enrollment.next_run_at is None
        assert not _due_step(tenancy.workspace).exists()

    def test_an_archived_sequence_refuses_new_subscribers(self, tenancy):
        from apps.campaigns.errors import SequenceNotRunnableError

        sequence = sequence_with(tenancy.workspace, steps=1)
        services.set_status(sequence, status=SequenceStatus.ARCHIVED)

        with pytest.raises(SequenceNotRunnableError):
            services.subscribe(sequence, contact_for(tenancy.workspace))

    def test_a_contact_from_another_workspace_is_refused(self, tenancy, other_tenancy):
        from apps.campaigns.errors import WorkspaceMismatchError

        sequence = sequence_with(tenancy.workspace, steps=1)

        with pytest.raises(WorkspaceMismatchError):
            services.subscribe(sequence, contact_for(other_tenancy.workspace))

    def test_re_enrollment_restarts_at_step_one_and_retires_the_previous_row(self, tenancy):
        """SPEC §12, and the partial unique index is what enforces it."""
        sequence = sequence_with(tenancy.workspace, steps=3)
        contact = contact_for(tenancy.workspace)
        first = services.subscribe(sequence, contact)
        _run_next_step(tenancy.workspace)

        second = services.subscribe(sequence, contact)

        first.refresh_from_db()
        assert first.status == EnrollmentStatus.UNSUBSCRIBED
        assert second.pk != first.pk
        assert second.current_step == 1
        assert (
            SequenceEnrollment.objects.for_workspace(tenancy.workspace)
            .filter(sequence=sequence, contact=contact, status=EnrollmentStatus.ACTIVE)
            .count()
            == 1
        )

    def test_re_enrollment_cancels_the_previous_queue_row(self, tenancy):
        sequence = sequence_with(tenancy.workspace, steps=2)
        contact = contact_for(tenancy.workspace)
        services.subscribe(sequence, contact)
        stale = _due_step(tenancy.workspace).get()

        services.subscribe(sequence, contact)

        stale.refresh_from_db()
        assert stale.status == ActionStatus.CANCELLED


@pytest.mark.django_db
class TestRunningAStep:
    def test_it_starts_the_step_s_flow_and_advances(self, tenancy):
        sequence = sequence_with(tenancy.workspace, steps=2)
        contact = contact_for(tenancy.workspace)
        enrollment = services.subscribe(sequence, contact)

        assert _run_next_step(tenancy.workspace) == ActionStatus.DONE

        enrollment.refresh_from_db()
        assert enrollment.current_step == 2
        assert enrollment.status == EnrollmentStatus.ACTIVE
        assert enrollment.last_sent_at is not None
        assert "Onboarding step 1" in _tags(contact)

    def test_the_execution_is_stamped_with_the_sequence(self, tenancy):
        sequence = sequence_with(tenancy.workspace, steps=1)
        contact = contact_for(tenancy.workspace)
        services.subscribe(sequence, contact)

        _run_next_step(tenancy.workspace)

        execution = FlowExecution.objects.for_workspace(tenancy.workspace).get()
        assert execution.started_by == StartedBy.stamp(StartedBy.SEQUENCE, sequence.pk)

    def test_the_next_step_is_measured_from_this_step_s_send(self, tenancy):
        sequence = sequence_with(tenancy.workspace, steps=2, delay_value=1, delay_unit=DelayUnit.DAYS)
        enrollment = services.subscribe(sequence, contact_for(tenancy.workspace))

        _run_next_step(tenancy.workspace)

        enrollment.refresh_from_db()
        assert enrollment.next_run_at == enrollment.last_sent_at + timedelta(days=1)

    def test_the_last_step_completes_the_enrollment(self, tenancy):
        sequence = sequence_with(tenancy.workspace, steps=1)
        enrollment = services.subscribe(sequence, contact_for(tenancy.workspace))

        _run_next_step(tenancy.workspace)

        enrollment.refresh_from_db()
        assert enrollment.status == EnrollmentStatus.COMPLETED
        assert enrollment.next_run_at is None
        assert _due_step(tenancy.workspace).filter(status=ActionStatus.PENDING).count() == 0

    def test_a_stale_row_for_a_passed_position_is_dropped(self, tenancy):
        """Zombie recovery re-runs a handler whose work committed."""
        sequence = sequence_with(tenancy.workspace, steps=2)
        contact = contact_for(tenancy.workspace)
        services.subscribe(sequence, contact)
        _run_next_step(tenancy.workspace)
        replayed = _due_step(tenancy.workspace).order_by("run_at").first()
        replayed.status = ActionStatus.RUNNING
        replayed.payload = {**replayed.payload, "position": 1}
        replayed.save(update_fields=["status", "payload"])

        assert process_action(replayed) == ActionStatus.DONE

        # Step 1's flow ran once, not twice: one execution, and the enrollment
        # is still standing on step 2.
        assert FlowExecution.objects.for_workspace(tenancy.workspace).count() == 1

    def test_a_step_whose_flow_cannot_run_still_advances_the_sequence(self, tenancy, caplog):
        """Otherwise one unpublished flow parks every subscriber for ever."""
        from apps.flows.models import Flow, FlowStatus

        sequence = sequence_with(tenancy.workspace, steps=2)
        broken = sequence.steps.get(position=1).flow
        Flow.objects.for_workspace(tenancy.workspace).filter(pk=broken.pk).update(status=FlowStatus.ARCHIVED)
        enrollment = services.subscribe(sequence, contact_for(tenancy.workspace))

        with caplog.at_level("WARNING"):
            assert _run_next_step(tenancy.workspace) == ActionStatus.DONE

        enrollment.refresh_from_db()
        assert enrollment.current_step == 2
        assert "cannot start flow" in caplog.text

    def test_a_deleted_step_advances_rather_than_stalling(self, tenancy):
        sequence = sequence_with(tenancy.workspace, steps=2)
        enrollment = services.subscribe(sequence, contact_for(tenancy.workspace))
        sequence.steps.filter(position=1).delete()

        _run_next_step(tenancy.workspace)

        enrollment.refresh_from_db()
        assert enrollment.current_step == 2

    def test_a_row_naming_a_deleted_enrollment_is_dropped(self, tenancy):
        sequence = sequence_with(tenancy.workspace, steps=1)
        enrollment = services.subscribe(sequence, contact_for(tenancy.workspace))
        action = _due_step(tenancy.workspace).get()
        SequenceEnrollment.objects.for_workspace(tenancy.workspace).filter(pk=enrollment.pk).delete()
        action.status = ActionStatus.RUNNING
        action.save(update_fields=["status"])

        assert process_action(action) == ActionStatus.DONE


@pytest.mark.django_db
class TestUnsubscribing:
    def test_it_stops_future_steps(self, tenancy):
        sequence = sequence_with(tenancy.workspace, steps=3)
        contact = contact_for(tenancy.workspace)
        enrollment = services.subscribe(sequence, contact)
        _run_next_step(tenancy.workspace)

        services.unsubscribe(sequence, contact)

        enrollment.refresh_from_db()
        assert enrollment.status == EnrollmentStatus.UNSUBSCRIBED
        assert enrollment.next_run_at is None
        assert not _due_step(tenancy.workspace).filter(status=ActionStatus.PENDING).exists()

    def test_a_claimed_step_declines_rather_than_running_the_flow(self, tenancy):
        """The narrow race: a worker claimed the row before the unsubscribe."""
        sequence = sequence_with(tenancy.workspace, steps=2)
        contact = contact_for(tenancy.workspace)
        services.subscribe(sequence, contact)
        action = _due_step(tenancy.workspace).get()
        action.status = ActionStatus.RUNNING
        action.save(update_fields=["status"])
        services.unsubscribe(sequence, contact)

        assert process_action(action) == ActionStatus.DONE
        assert not FlowExecution.objects.for_workspace(tenancy.workspace).exists()

    def test_an_execution_already_running_completes(self, tenancy):
        """SPEC §12: unsubscribe stops future steps, mid-flight runs finish."""
        sequence = sequence_with(tenancy.workspace, steps=2)
        contact = contact_for(tenancy.workspace)
        services.subscribe(sequence, contact)
        _run_next_step(tenancy.workspace)
        execution = FlowExecution.objects.for_workspace(tenancy.workspace).get()

        services.unsubscribe(sequence, contact)

        execution.refresh_from_db()
        assert execution.status == ExecutionStatus.COMPLETED
        assert "Onboarding step 1" in _tags(contact)

    def test_unsubscribing_somebody_who_is_not_enrolled_is_a_quiet_no_op(self, tenancy):
        sequence = sequence_with(tenancy.workspace, steps=1)

        assert services.unsubscribe(sequence, contact_for(tenancy.workspace)) is None


@pytest.mark.django_db
class TestIdempotency:
    def test_a_second_enqueue_for_the_same_step_returns_the_same_row(self, tenancy):
        from apps.campaigns.scheduling import enqueue_step

        sequence = sequence_with(tenancy.workspace, steps=2)
        enrollment = services.subscribe(sequence, contact_for(tenancy.workspace))
        first = _due_step(tenancy.workspace).get()

        again = enqueue_step(enrollment)

        assert again.pk == first.pk
        assert _due_step(tenancy.workspace).count() == 1

    def test_a_completed_enrollment_enqueues_nothing(self, tenancy):
        from apps.campaigns.scheduling import enqueue_step

        sequence = sequence_with(tenancy.workspace, steps=0)
        enrollment = services.subscribe(sequence, contact_for(tenancy.workspace))

        assert enqueue_step(enrollment) is None


@pytest.mark.django_db
class TestSteps:
    def test_deleting_a_step_closes_the_gap_in_the_numbering(self, tenancy):
        sequence = sequence_with(tenancy.workspace, steps=3)

        services.delete_step(sequence.steps.get(position=2))

        assert list(sequence.steps.order_by("position").values_list("position", flat=True)) == [1, 2]

    def test_moving_a_step_swaps_it_with_its_neighbour(self, tenancy):
        sequence = sequence_with(tenancy.workspace, steps=3)
        third = sequence.steps.get(position=3)

        services.move_step(third, direction="up")

        assert third.position == 2
        assert sequence.steps.get(pk=third.pk).position == 2

    def test_moving_the_first_step_up_is_a_no_op(self, tenancy):
        sequence = sequence_with(tenancy.workspace, steps=2)
        first = sequence.steps.get(position=1)

        assert services.move_step(first, direction="up").position == 1

    def test_a_step_may_not_name_another_workspace_s_flow(self, tenancy, other_tenancy):
        from apps.campaigns.errors import WorkspaceMismatchError

        sequence = sequence_with(tenancy.workspace, steps=0)
        theirs = runnable_flow(other_tenancy.workspace)

        with pytest.raises(WorkspaceMismatchError):
            services.add_step(sequence, flow=theirs, delay_value=1, delay_unit=DelayUnit.DAYS)

    def test_activating_a_sequence_with_no_steps_is_refused(self, tenancy):
        from apps.campaigns.errors import SequenceNotRunnableError

        sequence = sequence_with(tenancy.workspace, steps=0)

        with pytest.raises(SequenceNotRunnableError):
            services.set_status(sequence, status=SequenceStatus.ACTIVE)


@pytest.mark.django_db
class TestRenumbering:
    def test_deleting_the_first_of_many_steps_closes_the_gap(self, tenancy):
        """A single decrementing UPDATE against `(sequence, position)` — which is
        NOT deferrable — is checked per row as it lands, and the row order is the
        planner's choice: ascending succeeds, descending raises a duplicate key.
        The two-pass renumber is what makes it order-independent."""
        sequence = sequence_with(tenancy.workspace, steps=6)

        services.delete_step(sequence.steps.get(position=1))

        assert list(sequence.steps.order_by("position").values_list("position", flat=True)) == [1, 2, 3, 4, 5]

    def test_deleting_from_the_middle_keeps_the_order_of_the_survivors(self, tenancy):
        sequence = sequence_with(tenancy.workspace, steps=5)
        keep = [step.flow_id for step in sequence.steps.order_by("position") if step.position != 3]

        services.delete_step(sequence.steps.get(position=3))

        assert [step.flow_id for step in sequence.steps.order_by("position")] == keep

    def test_deleting_the_last_step_leaves_the_rest_alone(self, tenancy):
        sequence = sequence_with(tenancy.workspace, steps=3)

        services.delete_step(sequence.steps.get(position=3))

        assert list(sequence.steps.order_by("position").values_list("position", flat=True)) == [1, 2]


@pytest.mark.django_db
class TestLostRaces:
    def test_a_duplicate_step_position_is_a_refusal_not_a_500(self, tenancy, monkeypatch):
        """Two editors adding a step at once both read the same count and pick
        the same position; the loser must hear a message, not poison the
        caller's transaction with an IntegrityError.

        The concurrent writer is simulated by taking the position in the window
        between the check and the write, which is exactly where the real race
        lives.
        """
        from apps.campaigns import services as campaign_services
        from apps.campaigns.errors import CampaignsError
        from apps.campaigns.models import SequenceStep

        sequence = sequence_with(tenancy.workspace, steps=1)
        squatter = runnable_flow(tenancy.workspace, name="Squatter")
        original = campaign_services._validated

        def take_the_position(step):
            monkeypatch.undo()
            SequenceStep.objects.create(
                workspace_id=tenancy.workspace.pk,
                sequence=sequence,
                position=step.position,
                flow=squatter,
                delay_value=1,
                delay_unit=DelayUnit.DAYS,
            )
            return original(step)

        monkeypatch.setattr(campaign_services, "_validated", take_the_position)

        with pytest.raises(CampaignsError):
            services.add_step(
                sequence, flow=runnable_flow(tenancy.workspace, name="Third"), delay_value=1, delay_unit=DelayUnit.DAYS
            )

    def test_a_lost_enrollment_race_is_a_refusal_not_a_500(self, tenancy, monkeypatch):
        """`_retire_active` then insert is a check-then-write against the partial
        unique index, and `contacts.bulk_sequence` runs subscribe in a loop — an
        IntegrityError there would poison the whole batch.

        Neutering the retire step is the same state a concurrent subscribe
        produces: an active row still standing when the insert lands.
        """
        from apps.campaigns import services as campaign_services
        from apps.campaigns.errors import CampaignsError

        sequence = sequence_with(tenancy.workspace, steps=2)
        contact = contact_for(tenancy.workspace)
        services.subscribe(sequence, contact)
        monkeypatch.setattr(campaign_services, "_retire_active", lambda sequence, contact: None)

        with pytest.raises(CampaignsError):
            services.subscribe(sequence, contact)

    def test_a_model_level_refusal_arrives_as_this_app_s_error(self, tenancy, other_tenancy):
        """`full_clean` raises ValidationError, which is not a ValueError — a view
        catching CampaignsError would answer 500 to a cross-workspace step."""
        from apps.campaigns.errors import CampaignsError

        sequence = sequence_with(tenancy.workspace, steps=0)

        with pytest.raises(CampaignsError):
            services.add_step(
                sequence,
                flow=runnable_flow(other_tenancy.workspace),
                delay_value=1,
                delay_unit=DelayUnit.DAYS,
            )


@pytest.mark.django_db
class TestADeletedContact:
    """`delete_contact` promises a tombstone is hidden everywhere."""

    def _enrolled(self, tenancy):
        sequence = sequence_with(tenancy.workspace, steps=3)
        contact = contact_for(tenancy.workspace)
        enrollment = services.subscribe(sequence, contact)
        return sequence, contact, enrollment

    def test_the_sweep_retires_the_enrollment_rather_than_churning_on_it(self, tenancy):
        """`activity.stand_down` cancels the queue row but knows nothing about
        enrollments, and re-enqueueing hits the same idempotency key and gets
        that cancelled row back — so without this it stays due for ever."""
        from apps.campaigns.housekeeping import sweep_sequence_enrollments
        from apps.contacts import activity

        sequence, contact, enrollment = self._enrolled(tenancy)
        activity.stand_down(contact)
        contact_services.delete_contact(contact)
        SequenceEnrollment.objects.for_workspace(tenancy.workspace).update(next_run_at=timezone.now())

        sweep_sequence_enrollments()

        enrollment.refresh_from_db()
        assert enrollment.status == EnrollmentStatus.UNSUBSCRIBED
        assert enrollment.next_run_at is None

    def test_a_claimed_step_retires_rather_than_running_the_flow(self, tenancy):
        sequence, contact, enrollment = self._enrolled(tenancy)
        action = _due_step(tenancy.workspace).get()
        contact_services.delete_contact(contact)
        action.status = ActionStatus.RUNNING
        action.save(update_fields=["status"])

        assert process_action(action) == ActionStatus.DONE

        enrollment.refresh_from_db()
        assert enrollment.status == EnrollmentStatus.UNSUBSCRIBED
        assert not FlowExecution.objects.for_workspace(tenancy.workspace).exists()

    def test_they_stop_counting_as_a_subscriber(self, tenancy):
        from apps.campaigns import selectors

        sequence, contact, _ = self._enrolled(tenancy)
        assert selectors.sequences_for(tenancy.workspace).get().subscriber_count == 1

        contact_services.delete_contact(contact)

        assert selectors.sequences_for(tenancy.workspace).get().subscriber_count == 0
        assert selectors.at_position(sequence) == {}
        assert selectors.subscribers_for(sequence).rows == []
