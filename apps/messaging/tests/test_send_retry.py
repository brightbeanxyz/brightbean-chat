"""The ``send_retry`` handler (SPEC §§9.4 and 9.5)."""

from datetime import timedelta
from typing import Any

import pytest
from django.utils import timezone

from apps.channels.events import OutboundMessage, SendResult, SendStatus, TextBlock
from apps.channels.providers.exceptions import APIError
from apps.channels.tests.fake_adapter import registered
from apps.common.platforms import Platform
from apps.contacts.services import create_contact
from apps.messaging import handlers, services
from apps.messaging.codes import Denial, Failure
from apps.messaging.handlers import MAX_SEND_ATTEMPTS
from apps.messaging.models import ContactChannelIdentity, Message, MessageStatus, OptInSource
from apps.queueing.models import ActionType, ScheduledAction
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

    def test_every_rung_of_the_ladder_is_reachable(
        self, tenancy: Any, contact: Any, connection: Any, identity: Any
    ) -> None:
        """SPEC §9.5 ends the ladder with 6h, and budgeting against the queue's
        DEFAULT_MAX_ATTEMPTS stopped one rung short — the fifth call was refused
        before next_run_at(5) could ever be asked for, so the last rung was dead
        code. A message gets its first call plus one per rung.
        """
        assert 1 + len(BACKOFF_SCHEDULE) == MAX_SEND_ATTEMPTS
        message = queued_message(tenancy, contact, connection)

        scheduled: list[int] = []
        for attempt in range(1, MAX_SEND_ATTEMPTS + 1):
            Message.objects.for_workspace(tenancy.workspace).filter(pk=message.pk).update(
                send_attempts=attempt, status=MessageStatus.QUEUED
            )
            message.refresh_from_db()
            before = timezone.now()
            action = handlers.schedule_send_retry(message)
            if action is None:
                break
            scheduled.append(round((action.run_at - before).total_seconds()))

        # Every rung, in order, and then it gives up.
        assert scheduled == [pytest.approx(rung, abs=2) for rung in BACKOFF_SCHEDULE]
        message.refresh_from_db()
        assert message.status == MessageStatus.FAILED
        assert message.error == Failure.RETRIES_EXHAUSTED

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
        self, tenancy: Any, contact: Any, connection: Any, identity: Any, monkeypatch: Any
    ) -> None:
        """Which is what the idempotency key is actually for.

        **The clock is held still, and that is the test rather than scenery.**
        The key is ``send_retry:{message}:{int(run_at.timestamp())}`` and each
        call works out its own ``run_at = now + delay``, so two calls that
        happen to straddle a whole second produce keys a second apart and two
        rows. Left on the wall clock this failed intermittently for a reason
        with nothing to do with what it asserts: "the same moment" is the
        premise, so the test has to actually supply one rather than hope for it.

        The only frozen clock in this suite, and the exception proves the rule.
        The project ships no freezer and moves time through the ORM instead
        (``apps/flows/tests/test_routing_pipeline.py``: "The clock is moved
        through the ORM"), which answers "has an hour passed?" and cannot
        express "did these two calls land in the same second?".
        """
        message = queued_message(tenancy, contact, connection)

        # After queued_message, so the send itself still runs on the real clock.
        frozen = timezone.now()
        monkeypatch.setattr(timezone, "now", lambda: frozen)

        first = handlers.schedule_send_retry(message, delay_seconds=60, use_backoff=False)
        second = handlers.schedule_send_retry(message, delay_seconds=60, use_backoff=False)

        assert first is not None and second is not None
        # Asserted before the pks: this is the mechanism, and a key shape that
        # stopped collapsing would otherwise show up only as a puzzling
        # duplicate row.
        assert first.idempotency_key == second.idempotency_key
        assert first.pk == second.pk

    def test_a_second_later_is_a_different_action(
        self, tenancy: Any, contact: Any, connection: Any, identity: Any, monkeypatch: Any
    ) -> None:
        """The key's granularity, pinned as a decision rather than left a surprise.

        One second is as close together as two callers can be and still be told
        apart, so two workers arming the same rung either side of a boundary do
        arm two actions. That is redundant work and not a correctness problem:
        both rows name the same contact, the worker takes the contact advisory
        lock for either (SPEC §9.6) so they cannot interleave, and by the time
        the second runs ``handle_send_retry`` re-reads the message and returns
        on anything but ``queued`` — with ``services._claim``'s compare-and-set
        behind that as the last word on who may call the provider. Only a real
        provider call spends a retry attempt, which is what SPEC §9.5's budget
        is counting.

        Widening the key to collapse those two would cost the property the
        class above exists for: a rate deferral spends no attempt, so keying on
        anything coarser than the run time risks a second deferral reusing the
        first one's key, ``schedule()`` handing back a completed row, and the
        message sitting queued forever with nothing to move it. The trade is
        deliberate; this test is where it is written down.
        """
        message = queued_message(tenancy, contact, connection)

        clock = timezone.now()
        monkeypatch.setattr(timezone, "now", lambda: clock)

        first = handlers.schedule_send_retry(message, delay_seconds=60, use_backoff=False)
        clock += timedelta(seconds=1)
        second = handlers.schedule_send_retry(message, delay_seconds=60, use_backoff=False)

        assert first is not None and second is not None
        assert first.idempotency_key != second.idempotency_key
        assert first.pk != second.pk

    def test_a_deferral_spends_no_retry_budget(
        self, tenancy: Any, contact: Any, connection: Any, identity: Any
    ) -> None:
        """No provider call happened, so there is nothing to charge for."""
        message = queued_message(tenancy, contact, connection)
        Message.objects.for_workspace(tenancy.workspace).filter(pk=message.pk).update(send_attempts=MAX_SEND_ATTEMPTS)
        message.refresh_from_db()
        assert handlers.schedule_send_retry(message, delay_seconds=1, use_backoff=False) is not None
        message.refresh_from_db()
        assert message.status == MessageStatus.QUEUED

    def test_a_provider_failure_still_gives_up_at_the_budget(
        self, tenancy: Any, contact: Any, connection: Any, identity: Any
    ) -> None:
        message = queued_message(tenancy, contact, connection)
        Message.objects.for_workspace(tenancy.workspace).filter(pk=message.pk).update(send_attempts=MAX_SEND_ATTEMPTS)
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

    def test_a_missing_identity_is_reported_as_such(
        self, tenancy: Any, contact: Any, connection: Any, identity: Any
    ) -> None:
        """Not as no_adapter. ``error`` is what an operator debugging a stuck
        send reads through codes.describe(), and the two causes send them to
        completely different places."""
        message = queued_message(tenancy, contact, connection)
        ContactChannelIdentity.objects.for_workspace(tenancy.workspace).filter(pk=identity.pk).delete()
        with registered(Platform.TELEGRAM) as adapter:
            run_retry(pending_action())
            assert adapter.sends == []
        message.refresh_from_db()
        assert message.status == MessageStatus.FAILED
        assert message.error == Denial.NO_IDENTITY

    def test_the_budget_is_counted_on_the_message(
        self, tenancy: Any, contact: Any, connection: Any, identity: Any
    ) -> None:
        """Not on the action: the *first* send is inline with no action row, so
        an action-based budget is off by one from birth."""
        message = queued_message(tenancy, contact, connection)
        assert message.send_attempts == 1

        Message.objects.for_workspace(tenancy.workspace).filter(pk=message.pk).update(send_attempts=MAX_SEND_ATTEMPTS)
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


class TestDeletedContacts:
    """The case a guard in ``send_outbound`` alone would have missed: the message
    was accepted while the contact was live, and the deletion happens while it
    sits on the backoff ladder. ``_dispatch`` is the last gate before the
    provider call, which is why the check lives there."""

    def test_a_retry_for_a_deleted_contact_never_reaches_the_provider(
        self, tenancy: Any, contact: Any, connection: Any, identity: Any
    ) -> None:
        from apps.contacts.services import delete_contact

        message = queued_message(tenancy, contact, connection)
        action = pending_action()
        delete_contact(contact)

        calls: list[Any] = []

        def record(_self: Any, c: Any, i: Any, o: Any) -> SendResult:
            calls.append(c)
            return SendResult(status=SendStatus.SENT, provider_message_id="pm-1")

        with registered(Platform.TELEGRAM) as adapter:
            adapter.send = record  # type: ignore[method-assign,assignment]
            handlers.handle_send_retry(action.payload, action)

        message.refresh_from_db()
        assert calls == []
        assert message.status == MessageStatus.FAILED
        assert message.error == Denial.CONTACT_DELETED

    def test_a_retry_for_a_live_contact_still_sends(
        self, tenancy: Any, contact: Any, connection: Any, identity: Any
    ) -> None:
        message = queued_message(tenancy, contact, connection)
        action = pending_action()

        def ok(_self: Any, c: Any, i: Any, o: Any) -> SendResult:
            return SendResult(status=SendStatus.SENT, provider_message_id="pm-1")

        with registered(Platform.TELEGRAM) as adapter:
            adapter.send = ok  # type: ignore[method-assign,assignment]
            handlers.handle_send_retry(action.payload, action)

        message.refresh_from_db()
        assert message.status == MessageStatus.SENT
