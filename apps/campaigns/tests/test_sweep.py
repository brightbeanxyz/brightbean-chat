"""The ``SKIP LOCKED`` reconciler (SPEC §12, issue #22's acceptance criteria).

The sweep is not the scheduler — every advance enqueues its own successor — so
what it has to be is *safe*: two workers running it at once over a thousand due
enrollments must not produce a second queue row for any of them, and must not
block on each other.

Both properties come from different mechanisms and both are asserted here. The
idempotency key is what makes a double enqueue impossible; ``SKIP LOCKED`` is
what makes the two workers useful rather than serialised.
"""

import threading

import pytest
from django.db import connection, connections, transaction
from django.utils import timezone

from apps.campaigns.housekeeping import BATCH_SIZE, sweep_sequence_enrollments
from apps.campaigns.models import EnrollmentStatus, SequenceEnrollment
from apps.campaigns.scheduling import idempotency_key_for
from apps.campaigns.tests.support import contact_for, sequence_with
from apps.contacts.models import Contact
from apps.queueing.models import ActionStatus, ActionType, ScheduledAction


def _enroll(workspace, sequence, count, *, due=True):
    """``count`` active enrollments, all standing on step 1, with no queue rows.

    Written straight through the model rather than through ``subscribe()``: the
    state the sweep exists for is "active, due, and nothing queued", which the
    service never produces because it always enqueues.
    """
    when = timezone.now() - timezone.timedelta(minutes=1) if due else timezone.now() + timezone.timedelta(days=1)
    contacts = Contact.objects.bulk_create(
        [Contact(workspace=workspace, first_name=f"C{index}") for index in range(count)]
    )
    rows = [
        SequenceEnrollment(
            workspace=workspace,
            sequence=sequence,
            contact=contact,
            current_step=1,
            next_run_at=when,
            status=EnrollmentStatus.ACTIVE,
        )
        for contact in contacts
    ]
    SequenceEnrollment.objects.bulk_create(rows)
    return rows


def _queued(workspace):
    return ScheduledAction.objects.for_workspace(workspace).filter(type=ActionType.SEQUENCE_STEP)


@pytest.mark.django_db
class TestTheSweep:
    def test_it_enqueues_a_due_enrollment_that_has_no_row(self, tenancy):
        sequence = sequence_with(tenancy.workspace, steps=2)
        (enrollment,) = _enroll(tenancy.workspace, sequence, 1)

        summary = sweep_sequence_enrollments()

        assert "1" in (summary or "")
        assert _queued(tenancy.workspace).get().idempotency_key == idempotency_key_for(enrollment, 1)

    def test_it_leaves_an_enrollment_that_is_not_due_alone(self, tenancy):
        sequence = sequence_with(tenancy.workspace, steps=2)
        _enroll(tenancy.workspace, sequence, 1, due=False)

        assert sweep_sequence_enrollments() is None
        assert not _queued(tenancy.workspace).exists()

    def test_it_leaves_an_unsubscribed_enrollment_alone(self, tenancy):
        sequence = sequence_with(tenancy.workspace, steps=2)
        (enrollment,) = _enroll(tenancy.workspace, sequence, 1)
        SequenceEnrollment.objects.for_workspace(tenancy.workspace).filter(pk=enrollment.pk).update(
            status=EnrollmentStatus.UNSUBSCRIBED
        )

        sweep_sequence_enrollments()

        assert not _queued(tenancy.workspace).exists()

    def test_running_it_twice_produces_one_row_per_enrollment(self, tenancy):
        """The idempotency key, not the lock, is what guarantees this."""
        sequence = sequence_with(tenancy.workspace, steps=2)
        _enroll(tenancy.workspace, sequence, 5)

        sweep_sequence_enrollments()
        sweep_sequence_enrollments()

        assert _queued(tenancy.workspace).count() == 5

    def test_it_does_not_disturb_a_row_the_normal_path_already_queued(self, tenancy):
        from apps.campaigns import services

        sequence = sequence_with(tenancy.workspace, steps=2)
        services.subscribe(sequence, contact_for(tenancy.workspace))
        queued = _queued(tenancy.workspace).get()
        SequenceEnrollment.objects.for_workspace(tenancy.workspace).update(next_run_at=timezone.now())

        sweep_sequence_enrollments()

        assert _queued(tenancy.workspace).count() == 1
        assert _queued(tenancy.workspace).get().pk == queued.pk

    def test_it_drains_more_than_one_batch(self, tenancy):
        sequence = sequence_with(tenancy.workspace, steps=2)
        _enroll(tenancy.workspace, sequence, BATCH_SIZE + 3)

        sweep_sequence_enrollments()

        assert _queued(tenancy.workspace).count() == BATCH_SIZE + 3


@pytest.mark.django_db(transaction=True)
class TestTwoWorkers:
    """Two real connections, so ``FOR UPDATE ... SKIP LOCKED`` actually applies.

    ``transaction=True`` because the default test transaction is invisible to a
    second connection: without it the thread below would see an empty table and
    the test would pass for the wrong reason.
    """

    ENROLLMENTS = 1_000

    def test_a_thousand_enrollments_swept_twice_over_fire_once_each(self, tenancy):
        sequence = sequence_with(tenancy.workspace, steps=2)
        _enroll(tenancy.workspace, sequence, self.ENROLLMENTS)
        failures: list[BaseException] = []

        def worker():
            try:
                sweep_sequence_enrollments()
            except BaseException as exc:  # pragma: no cover - reported below
                failures.append(exc)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert not failures, failures
        rows = _queued(tenancy.workspace)
        assert rows.count() == self.ENROLLMENTS
        keys = set(rows.values_list("idempotency_key", flat=True))
        assert len(keys) == self.ENROLLMENTS
        assert rows.filter(status=ActionStatus.PENDING).count() == self.ENROLLMENTS

    def test_skip_locked_means_the_second_sweeper_does_not_block(self, tenancy):
        """A held row lock must be stepped over, not waited on.

        Asserted structurally rather than by timing: the first transaction locks
        one enrollment and holds it, and the sweep running beside it still gets
        through every *other* row. Without SKIP LOCKED it would block on the
        first one until the outer transaction ended.
        """
        sequence = sequence_with(tenancy.workspace, steps=2)
        rows = _enroll(tenancy.workspace, sequence, 4)

        with transaction.atomic():
            list(
                SequenceEnrollment.objects.unscoped()
                .filter(pk=rows[0].pk)
                .select_for_update(of=("self",))
                .order_by("pk")
            )
            done = threading.Event()

            def worker():
                try:
                    sweep_sequence_enrollments()
                finally:
                    connections.close_all()
                    done.set()

            thread = threading.Thread(target=worker)
            thread.start()
            assert done.wait(timeout=20), "the sweep blocked on a locked row"
            thread.join()

        assert _queued(tenancy.workspace).count() == 3
        assert connection.vendor == "postgresql"
