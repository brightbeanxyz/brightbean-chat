"""The messaging service facade (ROADMAP contract 1, SPEC §§9.4 and 9.5)."""

import inspect
import threading
from datetime import timedelta
from typing import Any

import pytest
from django.db import connection as db_connection
from django.test import override_settings
from django.utils import timezone

from apps.channels.events import OutboundMessage, SendResult, SendStatus, TextBlock
from apps.channels.providers.exceptions import APIError, RateLimitError
from apps.channels.tests.fake_adapter import registered
from apps.common.platforms import Platform
from apps.contacts.services import create_contact
from apps.messaging import services
from apps.messaging.codes import Denial, Failure
from apps.messaging.models import (
    ContactChannelIdentity,
    Conversation,
    ConversationState,
    Message,
    MessageStatus,
    OptInSource,
    SendBucket,
)
from apps.messaging.tests.conftest import make_connection
from apps.queueing.models import DEFAULT_MAX_ATTEMPTS, ActionType, ScheduledAction

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


def send(tenancy: Any, contact: Any, connection: Any, **kwargs: Any) -> Message:
    kwargs.setdefault("source", "automation")
    kwargs.setdefault("idempotency_key", "k1")
    return services.send_outbound(
        workspace=tenancy.workspace,
        contact=contact,
        connection=connection,
        outbound=kwargs.pop("outbound", TEXT),
        **kwargs,
    )


class TestTheContract:
    def test_the_signatures_are_the_ones_two_other_workstreams_wrote_against(self) -> None:
        """L3-B and L4-D code against contract 1 without reading this app, so a
        rename has to break here rather than in their trees."""
        signature = inspect.signature(services.send_outbound)
        assert list(signature.parameters) == [
            "workspace",
            "contact",
            "connection",
            "outbound",
            "source",
            "idempotency_key",
            "blocking",
            "internal",
        ]
        for name in ("workspace", "contact", "connection", "outbound", "source", "idempotency_key"):
            assert signature.parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
            assert signature.parameters[name].default is inspect.Parameter.empty

    def test_the_rest_of_the_facade_exists(self) -> None:
        for name in (
            "upsert_contact_identity",
            "open_conversation",
            "close_conversation",
            "assign_conversation",
            "pause_automation",
        ):
            assert callable(getattr(services, name))

    def test_a_denial_never_raises(self, tenancy: Any, contact: Any, connection: Any) -> None:
        """Contract 1: denials come back as a failed row. A raise would kill the
        flow that a merely-refused send should not (SPEC §9.5)."""
        message = send(tenancy, contact, connection)
        assert message.status == MessageStatus.FAILED
        assert message.error == Denial.NO_IDENTITY


class TestIdempotency:
    def test_the_row_is_inserted_before_the_provider_is_called(
        self, tenancy: Any, contact: Any, connection: Any, identity: Any
    ) -> None:
        seen: list[bool] = []

        with registered(Platform.TELEGRAM) as adapter:

            def observe(self: Any, conn: Any, ident: Any, outbound: Any) -> SendResult:
                seen.append(Message.objects.for_workspace(tenancy.workspace).filter(idempotency_key="k1").exists())
                return SendResult(status=SendStatus.SENT, provider_message_id="pm-1")

            adapter.send = observe  # type: ignore[method-assign,assignment]
            send(tenancy, contact, connection)

        assert seen == [True]

    def test_the_same_key_never_calls_the_provider_twice(
        self, tenancy: Any, contact: Any, connection: Any, identity: Any
    ) -> None:
        with registered(Platform.TELEGRAM) as adapter:
            first = send(tenancy, contact, connection)
            second = send(tenancy, contact, connection)

        assert first.pk == second.pk
        assert len(adapter.sends) == 1
        assert Message.objects.for_workspace(tenancy.workspace).count() == 1

    def test_a_thousand_forced_retries_make_one_provider_call(
        self, tenancy: Any, contact: Any, connection: Any, identity: Any
    ) -> None:
        """SPEC §21's acceptance criterion, counted on the fake adapter."""
        with registered(Platform.TELEGRAM) as adapter:
            for _ in range(1000):
                send(tenancy, contact, connection)

        assert len(adapter.sends) == 1
        assert Message.objects.for_workspace(tenancy.workspace).count() == 1

    def test_a_different_key_is_a_different_message(
        self, tenancy: Any, contact: Any, connection: Any, identity: Any
    ) -> None:
        with registered(Platform.TELEGRAM) as adapter:
            # FakeAdapter returns a constant provider id; two real messages need
            # two, because (connection, provider_message_id) is unique.
            counter = iter(range(100))
            adapter.send = lambda _self, c, i, o: SendResult(  # type: ignore[method-assign,assignment]
                status=SendStatus.SENT, provider_message_id=f"pm-{next(counter)}"
            )
            first = send(tenancy, contact, connection, idempotency_key="k1")
            second = send(tenancy, contact, connection, idempotency_key="k2")
        assert first.pk != second.pk
        assert {first.status, second.status} == {MessageStatus.SENT}

    def test_a_reused_provider_id_does_not_raise_out_of_the_facade(
        self, tenancy: Any, contact: Any, connection: Any, identity: Any
    ) -> None:
        """The unique index on (connection, provider_message_id) is right, but a
        buggy adapter that reuses one must not turn a delivered message into a
        dead flow. The send happened; the id is what gets dropped."""
        with registered(Platform.TELEGRAM):
            first = send(tenancy, contact, connection, idempotency_key="k1")
            second = send(tenancy, contact, connection, idempotency_key="k2")
        assert first.provider_message_id == "fake-1"
        assert second.status == MessageStatus.SENT
        assert second.provider_message_id == ""


@pytest.mark.django_db(transaction=True)
class TestIdempotencyUnderConcurrency:
    def test_racing_callers_produce_one_row_and_one_call(self) -> None:
        """The unique index stops a second row; the dispatched_at claim stops a
        second *call*. Without the second guard every racing caller would read
        back the same queued row and all of them would send."""
        from tests.support import create_tenancy

        tenancy = create_tenancy("race")
        contact = create_contact(tenancy.workspace, first_name="Ada")
        connection = make_connection(tenancy.workspace, suffix="race")
        ContactChannelIdentity.objects.create(
            contact=contact,
            channel_connection=connection,
            platform=connection.platform,
            platform_user_id="u1",
            opt_in=True,
            opt_in_at=timezone.now(),
            opt_in_source=OptInSource.MESSAGE_IN,
        )
        barrier = threading.Barrier(8)

        with registered(Platform.TELEGRAM) as adapter:

            def attempt() -> None:
                try:
                    barrier.wait(timeout=10)
                    send(tenancy, contact, connection)
                finally:
                    db_connection.close()

            threads = [threading.Thread(target=attempt) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
                assert not thread.is_alive()

            assert len(adapter.sends) == 1

        assert Message.objects.for_workspace(tenancy.workspace).count() == 1


class TestFailurePolicy:
    """SPEC §9.5, one test per row of the table in services.py."""

    def _send_with(self, tenancy: Any, contact: Any, connection: Any, behaviour: Any) -> Message:
        """``registered()`` yields the adapter *class* and ``adapter_for()``
        instantiates it, so an override assigned here is an unbound method and
        takes ``self`` first. Wrapped rather than written that way at every call
        site, so the behaviours below read as ``(connection, identity, outbound)``."""
        with registered(Platform.TELEGRAM) as adapter:
            adapter.send = lambda _self, c, i, o: behaviour(c, i, o)  # type: ignore[method-assign,assignment]
            return send(tenancy, contact, connection)

    def test_a_sent_result_records_the_provider_id(
        self, tenancy: Any, contact: Any, connection: Any, identity: Any
    ) -> None:
        message = self._send_with(
            tenancy,
            contact,
            connection,
            lambda c, i, o: SendResult(status=SendStatus.SENT, provider_message_id="pm-9"),
        )
        assert message.status == MessageStatus.SENT
        assert message.provider_message_id == "pm-9"

    def test_a_failed_result_is_permanent(self, tenancy: Any, contact: Any, connection: Any, identity: Any) -> None:
        message = self._send_with(
            tenancy,
            contact,
            connection,
            lambda c, i, o: SendResult(status=SendStatus.FAILED, error="blocked_by_user"),
        )
        assert message.status == MessageStatus.FAILED
        assert message.error == f"{Failure.PROVIDER_REJECTED.value}:blocked_by_user"
        assert not ScheduledAction.objects.unscoped().filter(type=ActionType.SEND_RETRY).exists()

    def test_a_4xx_is_permanent(self, tenancy: Any, contact: Any, connection: Any, identity: Any) -> None:
        def reject(c: Any, i: Any, o: Any) -> SendResult:
            raise APIError("rejected", status_code=400, code="131047")

        message = self._send_with(tenancy, contact, connection, reject)
        assert message.status == MessageStatus.FAILED
        assert message.error == f"{Failure.PROVIDER_REJECTED.value}:131047"

    def test_a_5xx_is_queued_and_retried(self, tenancy: Any, contact: Any, connection: Any, identity: Any) -> None:
        def unavailable(c: Any, i: Any, o: Any) -> SendResult:
            raise APIError("boom", status_code=503)

        message = self._send_with(tenancy, contact, connection, unavailable)
        assert message.status == MessageStatus.QUEUED
        assert message.error.startswith(Failure.PROVIDER_UNAVAILABLE.value)
        assert ScheduledAction.objects.unscoped().filter(type=ActionType.SEND_RETRY).exists()

    def test_a_transport_failure_is_queued_and_retried(
        self, tenancy: Any, contact: Any, connection: Any, identity: Any
    ) -> None:
        """status_code is None for a timeout — unknown, so retryable."""

        def timeout(c: Any, i: Any, o: Any) -> SendResult:
            raise APIError("timed out")

        message = self._send_with(tenancy, contact, connection, timeout)
        assert message.status == MessageStatus.QUEUED

    def test_a_rate_limit_honours_retry_after(self, tenancy: Any, contact: Any, connection: Any, identity: Any) -> None:
        """RateLimitError is caught before APIError — it is a subclass, and the
        two are treated oppositely."""

        def throttled(c: Any, i: Any, o: Any) -> SendResult:
            raise RateLimitError(retry_after=120)

        before = timezone.now()
        message = self._send_with(tenancy, contact, connection, throttled)
        assert message.status == MessageStatus.QUEUED
        assert message.error == Failure.RATE_LIMITED
        action = ScheduledAction.objects.unscoped().filter(type=ActionType.SEND_RETRY).get()
        assert action.run_at >= before + timedelta(seconds=110)

    def test_an_adapter_bug_does_not_kill_the_flow(
        self, tenancy: Any, contact: Any, connection: Any, identity: Any
    ) -> None:
        def explode(c: Any, i: Any, o: Any) -> SendResult:
            raise ValueError("adapter bug")

        message = self._send_with(tenancy, contact, connection, explode)
        assert message.status == MessageStatus.QUEUED
        assert message.error == Failure.PROVIDER_UNAVAILABLE

    def test_a_missing_adapter_fails_the_message(
        self, tenancy: Any, contact: Any, connection: Any, identity: Any
    ) -> None:
        message = send(tenancy, contact, connection)
        assert message.status == MessageStatus.FAILED
        assert message.error == Failure.NO_ADAPTER

    def test_no_provider_prose_ever_lands_on_the_row(
        self, tenancy: Any, contact: Any, connection: Any, identity: Any
    ) -> None:
        """A provider's error text quotes the request that caused it, tokens
        included, and this column is rendered in the inbox (baseline §5)."""

        def leaky(c: Any, i: Any, o: Any) -> SendResult:
            raise APIError("POST https://api.example/bot123:SECRET/send failed", status_code=400)

        message = self._send_with(tenancy, contact, connection, leaky)
        assert "SECRET" not in message.error
        assert message.error == f"{Failure.PROVIDER_REJECTED.value}:400"


class TestIdempotencyKeyIsRequired:
    def test_an_empty_key_is_refused(self, tenancy: Any, contact: Any, connection: Any, identity: Any) -> None:
        """The unique constraint is partial — condition=~Q(idempotency_key="") —
        so a blank key is deduplicated by nothing and every call would make a
        fresh provider call, silently voiding the guarantee contract 1 rests on.
        Raising matches how an unknown ``source`` is already treated."""
        with registered(Platform.TELEGRAM) as adapter:
            with pytest.raises(ValueError, match="idempotency_key"):
                send(tenancy, contact, connection, idempotency_key="")
            assert adapter.sends == []
        assert Message.objects.for_workspace(tenancy.workspace).count() == 0


class TestDispatchOrdering:
    """Adapter, then claim, then token — nothing spends rate it cannot use."""

    def test_a_missing_adapter_spends_no_token_and_no_attempt(
        self, tenancy: Any, contact: Any, connection: Any, identity: Any
    ) -> None:
        message = send(tenancy, contact, connection)
        assert message.status == MessageStatus.FAILED
        assert message.error == Failure.NO_ADAPTER
        assert message.send_attempts == 0
        assert message.dispatched_at is None
        assert not SendBucket.objects.filter(connection=connection).exists()

    def test_a_caller_that_loses_the_claim_spends_no_token(
        self, tenancy: Any, contact: Any, connection: Any, identity: Any
    ) -> None:
        """Acquiring before claiming meant every racing caller burned a token
        and then discovered it had nothing to spend it on, draining the
        connection's bucket with calls that never happened."""
        with registered(Platform.TELEGRAM):
            first = send(tenancy, contact, connection)
        assert first.status == MessageStatus.SENT
        spent = SendBucket.objects.get(connection=connection).tokens

        # Force the row back to queued with its claim still held, the state a
        # second caller racing the first one sees.
        Message.objects.for_workspace(tenancy.workspace).filter(pk=first.pk).update(status=MessageStatus.QUEUED)
        first.refresh_from_db()
        with registered(Platform.TELEGRAM) as adapter:
            services._dispatch(first, connection, identity, TEXT, blocking=False)
            assert adapter.sends == []
        assert SendBucket.objects.get(connection=connection).tokens == spent

    @override_settings(DEFAULT_SEND_RATE_OVERRIDES={"telegram": 1}, SEND_BUCKET_BURST_SECONDS=1.0)
    def test_a_deferred_send_hands_its_claim_back(
        self, tenancy: Any, contact: Any, connection: Any, identity: Any
    ) -> None:
        """No call was made, so the next attempt must be able to take the claim
        and must not be charged an attempt for having waited."""
        with registered(Platform.TELEGRAM):
            send(tenancy, contact, connection, idempotency_key="a")
            deferred = send(tenancy, contact, connection, idempotency_key="b")
        assert deferred.status == MessageStatus.QUEUED
        assert deferred.error == Failure.RATE_DEFERRED
        assert deferred.dispatched_at is None
        assert deferred.send_attempts == 0


class TestThreadRecency:
    def test_a_sent_message_sets_the_threads_recency(
        self, tenancy: Any, contact: Any, connection: Any, identity: Any
    ) -> None:
        with registered(Platform.TELEGRAM):
            send(tenancy, contact, connection)
        assert Conversation.objects.for_workspace(tenancy.workspace).get().last_message_at is not None

    def test_a_refused_message_does_not(self, tenancy: Any, contact: Any, connection: Any) -> None:
        """SPEC §14 sorts the inbox on last_message_at. An opted-out contact
        targeted daily by an automation would otherwise float to the top of an
        agent's queue every day for messages that never left the building."""
        ContactChannelIdentity.objects.create(
            contact=contact,
            channel_connection=connection,
            platform=connection.platform,
            platform_user_id="u1",
            opt_in=False,
            opted_out_at=timezone.now(),
        )
        with registered(Platform.TELEGRAM):
            message = send(tenancy, contact, connection)
        assert message.status == MessageStatus.FAILED
        assert Conversation.objects.for_workspace(tenancy.workspace).get().last_message_at is None

    def test_an_internal_note_does(self, tenancy: Any, contact: Any, connection: Any, identity: Any) -> None:
        """A note is real thread content even though it is never sent."""
        services.send_as_agent(
            workspace=tenancy.workspace,
            contact=contact,
            connection=connection,
            outbound=TEXT,
            idempotency_key="note",
            internal=True,
        )
        assert Conversation.objects.for_workspace(tenancy.workspace).get().last_message_at is not None


class TestExhaustedBudget:
    """A message that runs out of attempts must end up failed, not queued.

    ``_schedule_retry`` fails the row itself when the budget is spent, and the
    finalise that used to follow it unconditionally put the row back to
    ``queued`` — with nothing scheduled to move it. The message sat there
    forever and the operator read the wrong status.
    """

    def _exhausted(self, tenancy: Any, contact: Any, connection: Any) -> Message:
        conversation = services.open_conversation(workspace=tenancy.workspace, contact=contact, connection=connection)
        return Message.objects.create(
            conversation=conversation,
            direction="out",
            source="automation",
            status=MessageStatus.QUEUED,
            idempotency_key="spent",
            # One short of the cap: the claim inside _dispatch takes it to five.
            send_attempts=DEFAULT_MAX_ATTEMPTS - 1,
        )

    def _last_attempt(self, tenancy: Any, contact: Any, connection: Any, behaviour: Any) -> Message:
        message = self._exhausted(tenancy, contact, connection)
        identity = ContactChannelIdentity.objects.for_workspace(tenancy.workspace).get()
        with registered(Platform.TELEGRAM) as adapter:
            adapter.send = behaviour  # type: ignore[method-assign,assignment]
            services._dispatch(message, connection, identity, TEXT, blocking=False)
        message.refresh_from_db()
        return message

    def test_a_5xx_on_the_last_attempt_fails_rather_than_queueing(
        self, tenancy: Any, contact: Any, connection: Any, identity: Any
    ) -> None:
        def unavailable(_self: Any, c: Any, i: Any, o: Any) -> SendResult:
            raise APIError("down", status_code=503)

        message = self._last_attempt(tenancy, contact, connection, unavailable)
        assert message.status == MessageStatus.FAILED
        assert message.error == Failure.RETRIES_EXHAUSTED
        assert not ScheduledAction.objects.unscoped().filter(type=ActionType.SEND_RETRY).exists()

    def test_a_rate_limit_on_the_last_attempt_fails_rather_than_queueing(
        self, tenancy: Any, contact: Any, connection: Any, identity: Any
    ) -> None:
        def throttled(_self: Any, c: Any, i: Any, o: Any) -> SendResult:
            raise RateLimitError(retry_after=30)

        message = self._last_attempt(tenancy, contact, connection, throttled)
        assert message.status == MessageStatus.FAILED
        assert message.error == Failure.RETRIES_EXHAUSTED

    def test_an_adapter_bug_on_the_last_attempt_fails_rather_than_queueing(
        self, tenancy: Any, contact: Any, connection: Any, identity: Any
    ) -> None:
        def explode(_self: Any, c: Any, i: Any, o: Any) -> SendResult:
            raise ValueError("adapter bug")

        message = self._last_attempt(tenancy, contact, connection, explode)
        assert message.status == MessageStatus.FAILED
        assert message.error == Failure.RETRIES_EXHAUSTED

    def test_no_message_is_ever_left_queued_with_nothing_scheduled(
        self, tenancy: Any, contact: Any, connection: Any, identity: Any
    ) -> None:
        """The invariant the bug broke, stated directly: a queued row always has
        an action that will come back to it."""

        def unavailable(_self: Any, c: Any, i: Any, o: Any) -> SendResult:
            raise APIError("down", status_code=503)

        self._last_attempt(tenancy, contact, connection, unavailable)
        for message in Message.objects.for_workspace(tenancy.workspace).filter(status=MessageStatus.QUEUED):
            assert (
                ScheduledAction.objects.unscoped()
                .filter(type=ActionType.SEND_RETRY, payload__message_id=str(message.pk))
                .exists()
            )


class TestSchedulingFailures:
    def test_a_retry_that_cannot_be_scheduled_fails_the_message(
        self, tenancy: Any, contact: Any, connection: Any, identity: Any, monkeypatch: Any
    ) -> None:
        """send_outbound promises never to raise for a send outcome, and "the
        retry could not be armed" is a send outcome — leaving the row queued and
        unreferenced would be the stuck-forever bug by another route."""

        def boom(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("queue is down")

        monkeypatch.setattr("apps.messaging.handlers.schedule_send_retry", boom)

        def unavailable(_self: Any, c: Any, i: Any, o: Any) -> SendResult:
            raise APIError("down", status_code=503)

        message = self._send(tenancy, contact, connection, unavailable)
        assert message.status == MessageStatus.FAILED
        assert message.error == Failure.RETRY_UNSCHEDULABLE

    def _send(self, tenancy: Any, contact: Any, connection: Any, behaviour: Any) -> Message:
        with registered(Platform.TELEGRAM) as adapter:
            adapter.send = behaviour  # type: ignore[method-assign,assignment]
            return send(tenancy, contact, connection)


class TestComplianceDenials:
    def _denied(self, tenancy: Any, contact: Any, connection: Any, **state: Any) -> Message:
        ContactChannelIdentity.objects.create(
            contact=contact,
            channel_connection=connection,
            platform=connection.platform,
            platform_user_id="u1",
            **state,
        )
        with registered(Platform.TELEGRAM) as adapter:
            message = send(tenancy, contact, connection)
            assert adapter.sends == []
        return message

    def test_an_opted_out_identity_produces_a_failed_row_and_no_call(
        self, tenancy: Any, contact: Any, connection: Any
    ) -> None:
        message = self._denied(tenancy, contact, connection, opt_in=False, opted_out_at=timezone.now())
        assert message.status == MessageStatus.FAILED
        assert message.error == Denial.OPTED_OUT

    def test_an_identity_with_no_consent_produces_a_failed_row_and_no_call(
        self, tenancy: Any, contact: Any, connection: Any
    ) -> None:
        message = self._denied(tenancy, contact, connection, opt_in=False)
        assert message.error == Denial.NO_OPT_IN

    def test_a_denial_is_never_silently_dropped(self, tenancy: Any, contact: Any, connection: Any) -> None:
        """The flow engine needs a row to follow its `default` edge from, and an
        operator needs to know what was refused."""
        self._denied(tenancy, contact, connection, opt_in=False)
        assert Message.objects.for_workspace(tenancy.workspace).count() == 1


class TestTheAgentPause:
    def test_an_agent_send_pauses_automation_for_thirty_minutes(
        self, tenancy: Any, contact: Any, connection: Any, identity: Any
    ) -> None:
        """SPEC §14, and contract 1 puts the write here rather than at the
        caller — a caller that forgot would leave automation replying over an
        agent mid-conversation."""
        before = timezone.now()
        with registered(Platform.TELEGRAM):
            services.send_as_agent(
                workspace=tenancy.workspace,
                contact=contact,
                connection=connection,
                outbound=TEXT,
                idempotency_key="agent-1",
            )
        conversation = Conversation.objects.for_workspace(tenancy.workspace).get()
        assert conversation.automation_paused_until is not None
        assert conversation.automation_paused_until >= before + timedelta(minutes=29)

    def test_automation_does_not_pause(self, tenancy: Any, contact: Any, connection: Any, identity: Any) -> None:
        with registered(Platform.TELEGRAM):
            send(tenancy, contact, connection)
        assert Conversation.objects.for_workspace(tenancy.workspace).get().automation_paused_until is None

    def test_a_blocked_agent_send_still_pauses(self, tenancy: Any, contact: Any, connection: Any) -> None:
        """The pause records an agent *taking over*, not a message going out."""
        ContactChannelIdentity.objects.create(
            contact=contact,
            channel_connection=connection,
            platform=connection.platform,
            platform_user_id="u1",
            opt_in=False,
            opted_out_at=timezone.now(),
        )
        services.send_as_agent(
            workspace=tenancy.workspace,
            contact=contact,
            connection=connection,
            outbound=TEXT,
            idempotency_key="agent-2",
        )
        assert Conversation.objects.for_workspace(tenancy.workspace).get().automation_paused_until is not None

    def test_it_never_shortens_a_longer_manual_pause(
        self, tenancy: Any, contact: Any, connection: Any, identity: Any
    ) -> None:
        """An operator who paused for two hours said something more deliberate
        than an agent typing a reply."""
        conversation = services.open_conversation(workspace=tenancy.workspace, contact=contact, connection=connection)
        long_pause = timezone.now() + timedelta(hours=2)
        services.pause_automation(conversation, long_pause)

        with registered(Platform.TELEGRAM):
            services.send_as_agent(
                workspace=tenancy.workspace,
                contact=contact,
                connection=connection,
                outbound=TEXT,
                idempotency_key="agent-3",
            )
        conversation.refresh_from_db()
        assert conversation.automation_paused_until == long_pause


class TestInternalNotes:
    def test_a_note_is_stored_and_never_sent(self, tenancy: Any, contact: Any, connection: Any, identity: Any) -> None:
        """SPEC §14: "internal notes (stored as message, direction out, flag
        internal, never sent)"."""
        with registered(Platform.TELEGRAM) as adapter:
            message = services.send_as_agent(
                workspace=tenancy.workspace,
                contact=contact,
                connection=connection,
                outbound=TEXT,
                idempotency_key="note-1",
                internal=True,
            )
            assert adapter.sends == []
        assert message.internal is True
        assert message.status == MessageStatus.SENT

    def test_a_note_skips_compliance_entirely(self, tenancy: Any, contact: Any, connection: Any) -> None:
        """There is nobody to be compliant towards: it never leaves the inbox."""
        ContactChannelIdentity.objects.create(
            contact=contact,
            channel_connection=connection,
            platform=connection.platform,
            platform_user_id="u1",
            opt_in=False,
            opted_out_at=timezone.now(),
        )
        message = services.send_as_agent(
            workspace=tenancy.workspace,
            contact=contact,
            connection=connection,
            outbound=TEXT,
            idempotency_key="note-2",
            internal=True,
        )
        assert message.internal is True
        assert message.error == ""


class TestConversations:
    def test_opening_is_idempotent(self, tenancy: Any, contact: Any, connection: Any) -> None:
        first = services.open_conversation(workspace=tenancy.workspace, contact=contact, connection=connection)
        second = services.open_conversation(workspace=tenancy.workspace, contact=contact, connection=connection)
        assert first.pk == second.pk

    def test_sending_reopens_a_closed_thread(self, tenancy: Any, contact: Any, connection: Any, identity: Any) -> None:
        """Leaving it done would hide the message from the list it belongs in."""
        conversation = services.open_conversation(workspace=tenancy.workspace, contact=contact, connection=connection)
        services.close_conversation(conversation)
        with registered(Platform.TELEGRAM):
            send(tenancy, contact, connection)
        conversation.refresh_from_db()
        assert conversation.state == ConversationState.OPEN

    def test_assigning_and_unassigning(self, tenancy: Any, contact: Any, connection: Any) -> None:
        conversation = services.open_conversation(workspace=tenancy.workspace, contact=contact, connection=connection)
        services.assign_conversation(conversation, tenancy.owner)
        assert conversation.assignee_id == tenancy.owner.pk
        services.assign_conversation(conversation, None)
        assert conversation.assignee_id is None


class TestUpsertContactIdentity:
    def test_it_records_the_consent_audit(self, tenancy: Any, contact: Any, connection: Any) -> None:
        identity = services.upsert_contact_identity(
            contact, connection.platform, "u1", source=OptInSource.DATA_COLLECTION, opt_in=True
        )
        assert identity.opt_in is True
        assert identity.opt_in_at is not None
        assert identity.opt_in_source == OptInSource.DATA_COLLECTION

    def test_it_attaches_the_workspaces_active_connection(self, tenancy: Any, contact: Any, connection: Any) -> None:
        identity = services.upsert_contact_identity(
            contact, connection.platform, "u1", source=OptInSource.API, opt_in=True
        )
        assert identity.channel_connection_id == connection.pk

    def test_with_no_connection_it_stores_a_pending_record(self, tenancy: Any, contact: Any) -> None:
        """Contract 1: an address captured before a connection exists."""
        identity = services.upsert_contact_identity(
            contact, Platform.SMS, "+15550101234", source=OptInSource.DATA_COLLECTION, opt_in=True
        )
        assert identity.is_pending

    def test_a_pending_record_is_upgraded_at_first_send(self, tenancy: Any, contact: Any) -> None:
        """The lazy upgrade, which is the whole reason the column is nullable."""
        pending = services.upsert_contact_identity(
            contact, Platform.SMS, "+15550101234", source=OptInSource.DATA_COLLECTION, opt_in=True
        )
        sms = make_connection(tenancy.workspace, platform=Platform.SMS, suffix="late")

        with registered(Platform.SMS) as adapter:
            message = services.send_outbound(
                workspace=tenancy.workspace,
                contact=contact,
                connection=sms,
                outbound=TEXT,
                source="automation",
                idempotency_key="upgraded",
            )
            assert len(adapter.sends) == 1

        pending.refresh_from_db()
        assert pending.channel_connection_id == sms.pk
        assert message.status == MessageStatus.SENT

    def test_it_never_re_grants_consent_after_an_opt_out(self, tenancy: Any, contact: Any, connection: Any) -> None:
        """Withdrawing consent is a deliberate act (SPEC §19); re-granting it
        has to be one too, and this function is called by imports and APIs."""
        identity = services.upsert_contact_identity(
            contact, connection.platform, "u1", source=OptInSource.API, opt_in=True
        )
        ContactChannelIdentity.objects.for_workspace(tenancy.workspace).filter(pk=identity.pk).update(
            opted_out_at=timezone.now(), opt_in=False
        )

        refreshed = services.upsert_contact_identity(
            contact, connection.platform, "u1", source=OptInSource.IMPORT, opt_in=True
        )
        assert refreshed.opt_in is False
        assert refreshed.opted_out_at is not None

    def test_it_keeps_the_moment_consent_was_first_given(self, tenancy: Any, contact: Any, connection: Any) -> None:
        first = services.upsert_contact_identity(
            contact, connection.platform, "u1", source=OptInSource.API, opt_in=True
        )
        again = services.upsert_contact_identity(
            contact, connection.platform, "u1", source=OptInSource.IMPORT, opt_in=True
        )
        assert again.opt_in_at == first.opt_in_at
        assert again.opt_in_source == OptInSource.API

    def test_an_unusable_address_is_refused(self, tenancy: Any, contact: Any) -> None:
        with pytest.raises(ValueError, match="needs an address"):
            services.upsert_contact_identity(contact, Platform.SMS, "  ", source="api", opt_in=True)


class TestRateDeferral:
    @override_settings(DEFAULT_SEND_RATE_OVERRIDES={"telegram": 1}, SEND_BUCKET_BURST_SECONDS=1.0)
    def test_an_empty_bucket_queues_rather_than_sending(
        self, tenancy: Any, contact: Any, connection: Any, identity: Any
    ) -> None:
        """SPEC §7.1: the inline path "falls back to enqueue when empty"."""
        with registered(Platform.TELEGRAM) as adapter:
            send(tenancy, contact, connection, idempotency_key="a")
            second = send(tenancy, contact, connection, idempotency_key="b")
            assert len(adapter.sends) == 1

        assert second.status == MessageStatus.QUEUED
        assert second.error == Failure.RATE_DEFERRED
        assert ScheduledAction.objects.unscoped().filter(type=ActionType.SEND_RETRY).count() == 1
