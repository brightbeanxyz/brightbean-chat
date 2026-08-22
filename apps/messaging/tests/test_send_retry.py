"""The ``send_retry`` handler (SPEC §§9.4 and 9.5)."""

from datetime import timedelta
from typing import Any

import pytest
from django.utils import timezone

from apps.channels.events import OutboundMessage, SendResult, TextBlock
from apps.channels.providers.exceptions import APIError
from apps.channels.tests.fake_adapter import registered
from apps.common.platforms import Platform
from apps.contacts.services import create_contact
from apps.messaging import handlers, services
from apps.messaging.codes import Denial, Failure
from apps.messaging.models import ContactChannelIdentity, Message, MessageStatus, OptInSource
from apps.queueing.models import DEFAULT_MAX_ATTEMPTS, ActionType, ScheduledAction
from apps.queueing.registry import get_handler
from apps.queueing.worker import BACKOFF_SCHEDULE

pytestmark = pytest.mark.django_db

TEXT = OutboundMessage(blocks=(TextBlock(text="hello"),))


@pytest.fixture
def contact(tenancy: Any) -> Any:
    return create_contact(tenancy.workspace, first_name="Ada")


@pytest.fixture
def identity(contact: Any, connection: Any) -> ContactChannelIdentity:
    return ContactChannelIdentity.objects.create(
        contact=contact,
        channel_connection=connection,
        platform=connection.platform,
        platform_user_id="u1",
        opt_in=True,
        opt_in_at=timezone.now(),
        opt_in_source=OptInSource.MESSAGE_IN,
        last_inbound_at=timezone.now(),
    )


def unavailable(_self: Any, c: Any, i: Any, o: Any) -> SendResult:
    raise APIError("down", status_code=503)


def queued_message(tenancy: Any, contact: Any, connection: Any, key: str = "k1") -> Message:
    """A message left ``queued`` by a retryable provider failure."""
    with registered(Platform.TELEGRAM) as adapter:
        adapter.send = unavailable  # type: ignore[method-assign,assignment]
        return services.send_outbound(
            workspace=tenancy.workspace,
            contact=contact,
            connection=connection,
            outbound=TEXT,
            source="automation",
            idempotency_key=key,
        )


def run_retry(action: ScheduledAction) -> None:
    handlers.handle_send_retry(action.payload, action)


def pending_action() -> ScheduledAction:
    return ScheduledAction.objects.unscoped().filter(type=ActionType.SEND_RETRY).latest("created_at")


class TestRegistration:
    def test_the_handler_is_registered_with_the_queue(self) -> None:
        assert get_handler(ActionType.SEND_RETRY) is handlers.handle_send_retry

    def test_the_backoff_ladder_is_not_restated_anywhere_in_this_app(self) -> None:
        """SPEC §9.5's 30s/2m/10m/1h/6h belongs to apps.queueing.worker. One
        definition, so this app and the worker cannot disagree about what "then
        failed" means."""
        import inspect
        from pathlib import Path

        assert BACKOFF_SCHEDULE == (30, 120, 600, 3600, 21600)
        app_dir = Path(inspect.getfile(handlers)).parent
        for path in app_dir.glob("*.py"):
            body = path.read_text()
            assert "21600" not in body, f"{path.name} restates the backoff ladder"


class TestScheduling:
    def test_a_retryable_failure_arms_the_next_attempt(
        self, tenancy: Any, contact: Any, connection: Any, identity: Any
    ) -> None:
        message = queued_message(tenancy, contact, connection)
        action = pending_action()
        assert action.payload == {"message_id": str(message.pk)}
        assert action.workspace_id == tenancy.workspace.pk

    def test_it_names_the_contact_so_the_worker_takes_the_lock(
        self, tenancy: Any, contact: Any, connection: Any, identity: Any
    ) -> None:
        """The worker takes the contact advisory lock for any action naming one,
        so this send cannot interleave with a flow step for the same person."""
        queued_message(tenancy, contact, connection)
        assert pending_action().contact_id == contact.pk

    def test_it_walks_the_queues_backoff_ladder(
        self, tenancy: Any, contact: Any, connection: Any, identity: Any
    ) -> None:
        before = timezone.now()
        queued_message(tenancy, contact, connection)
        action = pending_action()
        assert action.run_at >= before + timedelta(seconds=BACKOFF_SCHEDULE[0] - 5)
        assert action.run_at <= before + timedelta(seconds=BACKOFF_SCHEDULE[1])

    def test_a_hostile_retry_after_cannot_park_a_message_forever(
        self, tenancy: Any, contact: Any, connection: Any, identity: Any
    ) -> None:
        message = queued_message(tenancy, contact, connection)
        before = timezone.now()
        handlers.schedule_send_retry(message, delay_seconds=60 * 60 * 24 * 365)
        assert pending_action().run_at <= before + timedelta(seconds=handlers.MAX_RETRY_AFTER_SECONDS + 5)


class TestRepeatedRateDeferral:
    """A throttled connection must not deadlock itself.

    A rate deferral spends no send attempt, so an attempt-numbered action key
    would be reused on the second deferral — ``schedule()`` hands back the
    already-completed row rather than arming anything, and the message sits
    queued forever with nothing scheduled to move it. Keying on the run time
    instead is what makes each deferral a new action.
    """

    def test_deferring_twice_arms_two_actions(self, tenancy: Any, contact: Any, connection: Any, identity: Any) -> None:
        message = queued_message(tenancy, contact, connection)
        Message.objects.for_workspace(tenancy.workspace).filter(pk=message.pk).update(send_attempts=0)
        message.refresh_from_db()

        first = handlers.schedule_send_retry(message, delay_seconds=1, use_backoff=False)
        second = handlers.schedule_send_retry(message, delay_seconds=90, use_backoff=False)

        assert first is not None
        assert second is not None
        assert first.pk != second.pk

    def test_two_callers_arming_the_same_moment_collapse_into_one(
        self, tenancy: Any, contact: Any, connection: Any, identity: Any
    ) -> None:
        """Which is what the idempotency key is actually for."""
        message = queued_message(tenancy, contact, connection)
        first = handlers.schedule_send_retry(message, delay_seconds=60, use_backoff=False)
        second = handlers.schedule_send_retry(message, delay_seconds=60, use_backoff=False)
        assert first is not None and second is not None
        assert first.pk == second.pk

    def test_a_deferral_spends_no_retry_budget(
        self, tenancy: Any, contact: Any, connection: Any, identity: Any
    ) -> None:
        """No provider call happened, so there is nothing to charge for."""
        message = queued_message(tenancy, contact, connection)
        Message.objects.for_workspace(tenancy.workspace).filter(pk=message.pk).update(
            send_attempts=DEFAULT_MAX_ATTEMPTS
        )
        message.refresh_from_db()
        assert handlers.schedule_send_retry(message, delay_seconds=1, use_backoff=False) is not None
        message.refresh_from_db()
        assert message.status == MessageStatus.QUEUED

    def test_a_provider_failure_still_gives_up_at_the_budget(
        self, tenancy: Any, contact: Any, connection: Any, identity: Any
    ) -> None:
        message = queued_message(tenancy, contact, connection)
        Message.objects.for_workspace(tenancy.workspace).filter(pk=message.pk).update(
            send_attempts=DEFAULT_MAX_ATTEMPTS
        )
        message.refresh_from_db()
        assert handlers.schedule_send_retry(message) is None
        message.refresh_from_db()
        assert message.status == MessageStatus.FAILED
        assert message.error == Failure.RETRIES_EXHAUSTED


class TestTheHandler:
    def test_it_re_sends_with_the_same_idempotency_key(
        self, tenancy: Any, contact: Any, connection: Any, identity: Any
    ) -> None:
        message = queued_message(tenancy, contact, connection)
        with registered(Platform.TELEGRAM) as adapter:
            run_retry(pending_action())
            assert len(adapter.sends) == 1
        message.refresh_from_db()
        assert message.status == MessageStatus.SENT
        assert message.idempotency_key == "k1"
        assert Message.objects.for_workspace(tenancy.workspace).count() == 1

    def test_running_it_twice_makes_one_more_call(
        self, tenancy: Any, contact: Any, connection: Any, identity: Any
    ) -> None:
        """Zombie recovery can re-run an action whose handler already committed,
        so the terminal-status guard is what keeps that from double-sending."""
        queued_message(tenancy, contact, connection)
        action = pending_action()
        with registered(Platform.TELEGRAM) as adapter:
            run_retry(action)
            run_retry(action)
            assert len(adapter.sends) == 1

    def test_it_re_checks_compliance(self, tenancy: Any, contact: Any, connection: Any, identity: Any) -> None:
        """Six hours is the last rung and a 24-hour window closes inside it.
        This is the compliance chokepoint; a retry that skipped it would be the
        one path that sends outside a window without asking."""
        message = queued_message(tenancy, contact, connection)
        ContactChannelIdentity.objects.for_workspace(tenancy.workspace).filter(pk=identity.pk).update(
            opted_out_at=timezone.now(), opt_in=False
        )
        with registered(Platform.TELEGRAM) as adapter:
            run_retry(pending_action())
            assert adapter.sends == []
        message.refresh_from_db()
        assert message.status == MessageStatus.FAILED
        assert message.error == Denial.OPTED_OUT

    def test_the_budget_is_counted_on_the_message(
        self, tenancy: Any, contact: Any, connection: Any, identity: Any
    ) -> None:
        """Not on the action: the *first* send is inline with no action row, so
        an action-based budget is off by one from birth."""
        message = queued_message(tenancy, contact, connection)
        assert message.send_attempts == 1

        Message.objects.for_workspace(tenancy.workspace).filter(pk=message.pk).update(
            send_attempts=DEFAULT_MAX_ATTEMPTS
        )
        with registered(Platform.TELEGRAM) as adapter:
            run_retry(pending_action())
            assert adapter.sends == []
        message.refresh_from_db()
        assert message.status == MessageStatus.FAILED
        assert message.error == Failure.RETRIES_EXHAUSTED

    def test_it_ignores_a_message_that_already_finished(
        self, tenancy: Any, contact: Any, connection: Any, identity: Any
    ) -> None:
        message = queued_message(tenancy, contact, connection)
        action = pending_action()
        Message.objects.for_workspace(tenancy.workspace).filter(pk=message.pk).update(status=MessageStatus.SENT)
        with registered(Platform.TELEGRAM) as adapter:
            run_retry(action)
            assert adapter.sends == []

    def test_an_unknown_message_id_is_not_an_error(self, tenancy: Any) -> None:
        """Raising would retry work that cannot succeed."""
        action = ScheduledAction.objects.create(
            workspace=tenancy.workspace,
            run_at=timezone.now(),
            type=ActionType.SEND_RETRY,
            payload={"message_id": "01a02a00-0000-7000-8000-000000000000"},
        )
        run_retry(action)

    def test_it_cannot_reach_another_tenants_message(
        self, tenancy: Any, other_tenancy: Any, contact: Any, connection: Any, identity: Any
    ) -> None:
        """The lookup is scoped to the action's own workspace."""
        message = queued_message(tenancy, contact, connection)
        rival = ScheduledAction.objects.create(
            workspace=other_tenancy.workspace,
            run_at=timezone.now(),
            type=ActionType.SEND_RETRY,
            payload={"message_id": str(message.pk)},
        )
        with registered(Platform.TELEGRAM) as adapter:
            run_retry(rival)
            assert adapter.sends == []
        message.refresh_from_db()
        assert message.status == MessageStatus.QUEUED


class TestUnknownOutcome:
    """SPEC §9.4: dispatched with no provider id means the call went out and we
    never learned the result."""

    def test_a_message_that_never_dispatched_is_safe_to_re_send(
        self, tenancy: Any, contact: Any, connection: Any, identity: Any
    ) -> None:
        message = queued_message(tenancy, contact, connection)
        Message.objects.for_workspace(tenancy.workspace).filter(pk=message.pk).update(dispatched_at=None)
        with registered(Platform.TELEGRAM) as adapter:
            run_retry(pending_action())
            assert len(adapter.sends) == 1

    def test_a_lookup_capable_adapter_avoids_the_duplicate(
        self, tenancy: Any, contact: Any, connection: Any, identity: Any
    ) -> None:
        """The seam SPEC §9.4 names. No platform on the roadmap offers it, so
        this is the only place it is exercised — and the reason the re-send is
        documented rather than assumed."""
        message = queued_message(tenancy, contact, connection)
        with registered(Platform.TELEGRAM) as adapter:
            adapter.find_sent_message = lambda _self, c, key: "pm-found"  # type: ignore[attr-defined]
            run_retry(pending_action())
            assert adapter.sends == []
        message.refresh_from_db()
        assert message.status == MessageStatus.SENT
        assert message.provider_message_id == "pm-found"

    def test_a_lookup_that_finds_nothing_falls_through_to_a_re_send(
        self, tenancy: Any, contact: Any, connection: Any, identity: Any
    ) -> None:
        queued_message(tenancy, contact, connection)
        with registered(Platform.TELEGRAM) as adapter:
            adapter.find_sent_message = lambda _self, c, key: None  # type: ignore[attr-defined]
            run_retry(pending_action())
            assert len(adapter.sends) == 1
