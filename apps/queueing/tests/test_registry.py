"""The handler registry and ``schedule()`` — the queue's public API."""

import uuid
from datetime import timedelta
from typing import Any

import pytest
from django.utils import timezone

from apps.queueing.models import ActionStatus, ScheduledAction
from apps.queueing.registry import (
    DuplicateHandlerError,
    IdempotencyKeyConflictError,
    get_handler,
    purge_for_contact,
    register_handler,
    registered_types,
    schedule,
    schedule_system,
)
from apps.queueing.tests.support import temporary_handler
from tests.support import Tenancy


def _noop(payload: dict[str, Any], action: ScheduledAction) -> None:
    return None


class TestHandlerRegistry:
    def test_registration_makes_the_handler_findable(self) -> None:
        with temporary_handler("registry_probe", _noop):
            assert get_handler("registry_probe") is _noop
            assert "registry_probe" in registered_types()

    def test_a_second_app_claiming_one_type_raises(self) -> None:
        def other(payload: dict[str, Any], action: ScheduledAction) -> None:
            return None

        with temporary_handler("registry_probe", _noop), pytest.raises(DuplicateHandlerError) as excinfo:
            register_handler("registry_probe")(other)

        # The message has to name the incumbent, or the victim of the clash has
        # no way to find which app already took the name.
        assert "_noop" in str(excinfo.value)

    def test_replace_is_available_for_a_deliberate_override(self) -> None:
        def other(payload: dict[str, Any], action: ScheduledAction) -> None:
            return None

        with temporary_handler("registry_probe", _noop):
            register_handler("registry_probe", replace=True)(other)
            assert get_handler("registry_probe") is other

    def test_registering_the_same_function_twice_is_harmless(self) -> None:
        """Django imports an AppConfig's modules more than once in some reload paths."""
        with temporary_handler("registry_probe", _noop):
            register_handler("registry_probe")(_noop)
            assert get_handler("registry_probe") is _noop


@pytest.mark.django_db
class TestSchedule:
    def test_creates_a_pending_row(self, tenancy: Tenancy) -> None:
        run_at = timezone.now() + timedelta(minutes=5)
        with temporary_handler("registry_probe", _noop):
            action = schedule("registry_probe", run_at, {"a": 1}, workspace=tenancy.workspace)

        assert action.status == ActionStatus.PENDING
        assert action.payload == {"a": 1}
        assert action.run_at == run_at
        assert action.workspace_id == tenancy.workspace.pk
        assert action.attempts == 0

    def test_contact_accepts_an_id_a_string_or_an_object(self, tenancy: Tenancy) -> None:
        """L2-A and L3-B hold Contact instances; housekeeping holds nothing."""
        contact_id = uuid.uuid4()

        class FakeContact:
            pk = contact_id

        now = timezone.now()
        by_uuid = schedule("t", now, workspace=tenancy.workspace, contact=contact_id)
        by_str = schedule("t", now, workspace=tenancy.workspace, contact=str(contact_id))
        by_object = schedule("t", now, workspace=tenancy.workspace, contact=FakeContact())

        assert by_uuid.contact_id == by_str.contact_id == by_object.contact_id == contact_id

    def test_an_idempotency_conflict_is_a_silent_no_op(self, tenancy: Tenancy) -> None:
        now = timezone.now()
        first = schedule("t", now, {"n": 1}, workspace=tenancy.workspace, idempotency_key="key-1")
        second = schedule("t", now, {"n": 2}, workspace=tenancy.workspace, idempotency_key="key-1")

        assert second.pk == first.pk
        assert second.payload == {"n": 1}  # the original wins; this is a no-op, not an upsert
        assert ScheduledAction.objects.for_workspace(tenancy.workspace).count() == 1

    def test_a_completed_key_does_not_schedule_a_second_run(self, tenancy: Tenancy) -> None:
        """What an idempotency key means: this work has already been arranged."""
        now = timezone.now()
        first = schedule("t", now, workspace=tenancy.workspace, idempotency_key="key-1")
        first.status = ActionStatus.DONE
        first.save(update_fields=["status", "updated_at"])

        again = schedule("t", now, workspace=tenancy.workspace, idempotency_key="key-1")

        assert again.pk == first.pk
        assert again.status == ActionStatus.DONE

    def test_a_key_held_by_another_workspace_raises_rather_than_leaking(
        self, tenancy: Tenancy, other_tenancy: Tenancy
    ) -> None:
        """The key column is globally unique; returning the row would cross tenants."""
        now = timezone.now()
        schedule("t", now, workspace=other_tenancy.workspace, idempotency_key="shared")

        with pytest.raises(IdempotencyKeyConflictError):
            schedule("t", now, workspace=tenancy.workspace, idempotency_key="shared")

    def test_the_conflict_does_not_poison_a_callers_transaction(self, tenancy: Tenancy) -> None:
        """A caller wrapping several enqueues in atomic() must survive a conflict.

        Without the savepoint inside ``schedule()`` the IntegrityError would
        leave the outer transaction unusable, and the caller's next query would
        fail with a bewildering "current transaction is aborted".
        """
        from django.db import transaction

        now = timezone.now()
        with transaction.atomic():
            schedule("t", now, workspace=tenancy.workspace, idempotency_key="key-1")
            schedule("t", now, workspace=tenancy.workspace, idempotency_key="key-1")
            still_usable = schedule("t", now, workspace=tenancy.workspace, idempotency_key="key-2")

        assert ScheduledAction.objects.for_workspace(tenancy.workspace).count() == 2
        assert still_usable.idempotency_key == "key-2"

    def test_an_unrelated_integrity_error_is_not_disguised_as_a_key_conflict(
        self, tenancy: Tenancy, monkeypatch: Any
    ) -> None:
        """A bare `except IntegrityError` reported every constraint failure as
        an idempotency collision, sending the reader after a key clash that
        never happened."""
        from django.db import IntegrityError

        def explode(*args: Any, **kwargs: Any) -> None:
            raise IntegrityError('insert violates foreign key constraint "…workspace_id…"')

        monkeypatch.setattr(ScheduledAction.objects, "create", explode)

        with pytest.raises(IntegrityError, match="foreign key"):
            schedule("t", timezone.now(), workspace=tenancy.workspace, idempotency_key="key-1")

    def test_a_real_key_conflict_is_still_recognised(self, tenancy: Tenancy) -> None:
        """The narrowed catch must still see the constraint it is there for."""
        first = schedule("t", timezone.now(), workspace=tenancy.workspace, idempotency_key="key-1")
        again = schedule("t", timezone.now(), workspace=tenancy.workspace, idempotency_key="key-1")

        assert again.pk == first.pk

    def test_scheduling_an_unhandled_type_warns_but_succeeds(self, tenancy: Tenancy, caplog: Any) -> None:
        """Enqueue must not depend on import order between two apps."""
        with caplog.at_level("WARNING", logger="apps.queueing.registry"):
            action = schedule("nobody_handles_this", timezone.now(), workspace=tenancy.workspace)

        assert action.pk is not None
        assert "nobody_handles_this" in caplog.text


@pytest.mark.django_db
class TestTheSystemBoundary:
    """A NULL workspace is invisible to every tenant query, so reaching it is
    a separate call rather than a falsy argument — the same reasoning that makes
    ``.unscoped()`` its own greppable method (CONTRIBUTING.md)."""

    def test_schedule_refuses_a_none_workspace(self, tenancy: Tenancy) -> None:
        """``request.workspace`` is None on every anonymous and non-/w/ request.

        Passing it through would mint a tenant's work as a system row its owner
        can never see or cancel — silent, and in the unsafe direction.
        """
        with pytest.raises(ValueError, match="needs a workspace"):
            schedule("t", timezone.now(), workspace=None)

        assert ScheduledAction.objects.unscoped().count() == 0

    def test_the_error_names_the_alternative(self) -> None:
        with pytest.raises(ValueError, match="schedule_system"):
            schedule("t", timezone.now(), workspace=None)

    def test_schedule_system_creates_the_deployment_level_row(self) -> None:
        action = schedule_system("housekeeping", timezone.now(), idempotency_key="sys-1")

        assert action.workspace_id is None
        again = schedule_system("housekeeping", timezone.now(), idempotency_key="sys-1")
        assert again.pk == action.pk

    def test_a_system_row_is_invisible_to_the_tenant_it_would_belong_to(self, tenancy: Tenancy) -> None:
        schedule_system("housekeeping", timezone.now())

        assert ScheduledAction.objects.for_workspace(tenancy.workspace).count() == 0
        assert ScheduledAction.objects.unscoped().count() == 1

    def test_schedule_system_takes_no_contact(self) -> None:
        """Contacts belong to workspaces; a system row has none, so the
        parameter is absent rather than ignored."""
        import inspect

        assert "contact" not in inspect.signature(schedule_system).parameters


@pytest.mark.django_db
class TestPurgeForContact:
    """Issue #29's erasure-side counterpart to ``cancel_pending``.

    A delete rather than an update, because a cancelled row is a row that still
    holds whatever its ``payload`` held — and for a GDPR erasure the payload is
    the point.
    """

    def action(self, workspace: Any, **kwargs: Any) -> ScheduledAction:
        defaults: dict[str, Any] = {
            "workspace": workspace,
            "run_at": timezone.now(),
            "type": "send_retry",
            "status": ActionStatus.PENDING,
        }
        defaults.update(kwargs)
        return ScheduledAction.objects.create(**defaults)

    def test_it_deletes_rows_naming_the_contact_in_the_column(self, tenancy: Any) -> None:
        contact_id = uuid.uuid4()
        row = self.action(tenancy.workspace, contact_id=contact_id)

        result = purge_for_contact(tenancy.workspace, contact_id)

        assert result.deleted == 1
        assert not ScheduledAction.objects.unscoped().filter(pk=row.pk).exists()

    def test_it_deletes_rows_naming_the_contact_in_the_payload(self, tenancy: Any) -> None:
        """``enqueue_delivery`` leaves the column null on purpose and puts the id
        under ``payload["data"]``; matching only the column would leave a
        webhook that fires after the erasure."""
        contact_id = uuid.uuid4()
        row = self.action(
            tenancy.workspace,
            contact_id=None,
            type="webhook_delivery",
            payload={"event": "contact.tag_added", "data": {"contact_id": str(contact_id)}},
        )

        result = purge_for_contact(tenancy.workspace, contact_id)

        assert result.deleted == 1
        assert not ScheduledAction.objects.unscoped().filter(pk=row.pk).exists()

    def test_it_deletes_terminal_rows_too(self, tenancy: Any) -> None:
        """A done or failed row still holds its payload."""
        contact_id = uuid.uuid4()
        for status in (ActionStatus.DONE, ActionStatus.FAILED, ActionStatus.CANCELLED):
            self.action(tenancy.workspace, contact_id=contact_id, status=status)

        assert purge_for_contact(tenancy.workspace, contact_id).deleted == 3

    def test_it_leaves_running_rows_alone_and_counts_them(self, tenancy: Any) -> None:
        """The privacy gap this return type exists to surface.

        Holding the contact lock stops a *new* handler starting; it says nothing
        about a row a worker marked running before its process died. That row
        survives with its payload, so the caller has to be able to say so rather
        than report a clean sweep.
        """
        contact_id = uuid.uuid4()
        zombie = self.action(tenancy.workspace, contact_id=contact_id, status=ActionStatus.RUNNING)
        self.action(tenancy.workspace, contact_id=contact_id)

        result = purge_for_contact(tenancy.workspace, contact_id)

        assert result.deleted == 1
        assert result.left_running == 1
        assert ScheduledAction.objects.unscoped().filter(pk=zombie.pk).exists()

    def test_it_honours_exclude_action_id(self, tenancy: Any) -> None:
        """Proven independently of the RUNNING exclusion that currently shadows
        it: on the queued path the worker has already marked the erasure's own
        action running, so without this test the parameter would be untested
        and the day the RUNNING rule changes it would be load-bearing and
        unproven."""
        contact_id = uuid.uuid4()
        spared = self.action(tenancy.workspace, contact_id=contact_id, type="contact_erasure")
        doomed = self.action(tenancy.workspace, contact_id=contact_id)

        result = purge_for_contact(tenancy.workspace, contact_id, exclude_action_id=spared.pk)

        assert result.deleted == 1
        assert ScheduledAction.objects.unscoped().filter(pk=spared.pk).exists()
        assert not ScheduledAction.objects.unscoped().filter(pk=doomed.pk).exists()

    def test_it_leaves_another_contacts_rows_alone(self, tenancy: Any) -> None:
        mine, theirs = uuid.uuid4(), uuid.uuid4()
        self.action(tenancy.workspace, contact_id=mine)
        survivor = self.action(tenancy.workspace, contact_id=theirs)

        assert purge_for_contact(tenancy.workspace, mine).deleted == 1
        assert ScheduledAction.objects.unscoped().filter(pk=survivor.pk).exists()

    def test_it_leaves_rows_naming_no_contact_alone(self, tenancy: Any) -> None:
        """A housekeeping row, or a ``broadcast.finished`` delivery."""
        contact_id = uuid.uuid4()
        system = self.action(tenancy.workspace, contact_id=None, type="housekeeping")

        assert purge_for_contact(tenancy.workspace, contact_id).deleted == 0
        assert ScheduledAction.objects.unscoped().filter(pk=system.pk).exists()

    def test_it_is_scoped_to_the_workspace(self, tenancy: Any, other_tenancy: Any) -> None:
        contact_id = uuid.uuid4()
        theirs = self.action(other_tenancy.workspace, contact_id=contact_id)

        assert purge_for_contact(tenancy.workspace, contact_id).deleted == 0
        assert ScheduledAction.objects.unscoped().filter(pk=theirs.pk).exists()
