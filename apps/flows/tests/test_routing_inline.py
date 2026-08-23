"""SPEC §7.1 step 4: what runs in the request, and what does not."""

import threading
import time
from datetime import timedelta

import pytest
from django.core.cache import cache
from django.db import connections, transaction
from django.utils import timezone

from apps.common.platforms import Platform
from apps.flows.models import FlowExecution, Trigger, TriggerType
from apps.flows.tests.routing_support import routing_adapter
from apps.flows.tests.support import connection_for, contact_for, graph, inbound, node, published_flow
from apps.flows.triggers.budget import (
    InlineBudget,
    InlineDecision,
    clear_slow_connection,
    connection_is_slow,
    has_send_capacity,
    may_run_inline,
    note_inline_latency,
)
from apps.flows.triggers.handlers import ROUTE_EVENT
from apps.flows.triggers.pipeline import route_events
from apps.queueing.models import ScheduledAction

SEND = {"blocks": [{"type": "text", "text": "hello"}]}
DELAY = {"mode": "duration", "duration": {"value": 1, "unit": "hours"}}


def _identity(connection, contact, user="tg-1"):
    from apps.messaging.models import ContactChannelIdentity

    identity = ContactChannelIdentity(
        contact=contact,
        channel_connection=connection,
        platform_user_id=user,
        opt_in=True,
        opt_in_at=timezone.now(),
        opt_in_source="message_in",
    )
    identity.save()
    return identity


def _keyword_flow(workspace, document, name="Reply"):
    return published_flow(workspace, document, name=name)


def _trigger(flow, word="help"):
    trigger = Trigger(
        flow=flow,
        type=TriggerType.KEYWORD,
        config_json={"keywords": [{"text": word, "mode": "contains"}]},
    )
    trigger.save()
    return trigger


def _bucket(connection, *, tokens: float, refilled_at=None):
    """The send bucket for this connection, as the send pipeline would leave it."""
    from apps.messaging.buckets import capacity_for, rate_for
    from apps.messaging.models import SendBucket

    rate = rate_for(connection.platform)
    return SendBucket.objects.create(
        connection=connection,
        tokens=tokens,
        capacity=capacity_for(rate),
        refill_rate=rate,
        refilled_at=refilled_at or timezone.now(),
    )


def _drain(connection, *, refilled_at=None):
    return _bucket(connection, tokens=0, refilled_at=refilled_at)


def _actions(workspace):
    return ScheduledAction.objects.for_workspace(workspace).filter(type=ROUTE_EVENT)


class TestTheBudget:
    def test_it_counts_down(self):
        budget = InlineBudget.start(0.05)
        assert budget.allows(0.01)
        time.sleep(0.06)
        assert budget.exhausted()

    def test_the_worker_has_no_deadline(self):
        """A worker has no client on the other end of a socket."""
        budget = InlineBudget.unbounded()
        assert budget.exhausted() is False
        assert budget.allows(3600)

    def test_one_budget_covers_a_whole_batch(self):
        """A Meta delivery carrying ten events must not get ten times 1.5 s."""
        budget = InlineBudget.start(0.05)
        first = budget.remaining()
        time.sleep(0.02)
        assert budget.remaining() < first


@pytest.mark.django_db
class TestTheGates:
    def test_an_unsafe_first_step_enqueues(self, tenancy, connection):
        decision = may_run_inline(connection, InlineBudget.start(), first_step_safe=False)
        assert decision is InlineDecision.NOT_SYNCHRONOUS_SAFE

    def test_an_exhausted_budget_enqueues(self, tenancy, connection):
        decision = may_run_inline(connection, InlineBudget.start(-1), first_step_safe=True)
        assert decision is InlineDecision.BUDGET

    def test_a_bucket_with_no_row_yet_is_full(self, tenancy, connection):
        """A connection that has never sent has a full bucket — the row is
        created full at the first send, so the absence of one is not empty."""
        assert has_send_capacity(connection) is True

    def test_a_drained_bucket_enqueues(self, tenancy, connection):
        _drain(connection)

        assert may_run_inline(connection, InlineBudget.start(), first_step_safe=True) is InlineDecision.NO_SEND_CAPACITY

    def test_the_capacity_probe_leaves_the_row_untouched(self, tenancy, connection):
        """``send_outbound`` performs the one real debit, and it does so under a
        row lock. A gate that went through ``try_acquire`` would take that lock
        and rewrite the row for *every* inbound event — serialising a busy
        connection's events on one row inside SPEC §7.1's budget — so this reads
        and writes nothing at all."""
        from apps.messaging.models import SendBucket

        bucket = _bucket(connection, tokens=5)
        before = SendBucket.objects.values("tokens", "refilled_at", "updated_at").get(pk=bucket.pk)

        for _ in range(3):
            assert has_send_capacity(connection) is True

        after = SendBucket.objects.values("tokens", "refilled_at", "updated_at").get(pk=bucket.pk)
        assert after == before

    def test_the_probe_counts_the_refill_since_the_last_send(self, tenancy, connection):
        """It mirrors ``buckets._spend``'s arithmetic, so a bucket drained a
        while ago reads as available again without anyone writing to it."""
        _drain(connection, refilled_at=timezone.now() - timedelta(seconds=30))

        assert has_send_capacity(connection) is True

    def test_a_slow_connection_is_flagged_and_then_enqueues(self, tenancy, connection):
        clear_slow_connection(connection)
        assert connection_is_slow(connection) is False

        note_inline_latency(connection, 99.0)

        assert connection_is_slow(connection) is True
        assert may_run_inline(connection, InlineBudget.start(), first_step_safe=True) is InlineDecision.SLOW_CONNECTION

    def test_a_fast_call_does_not_flag_the_connection(self, tenancy, connection):
        clear_slow_connection(connection)
        note_inline_latency(connection, 0.01)
        assert connection_is_slow(connection) is False


@pytest.mark.django_db
class TestSafetyCheck:
    def test_an_event_type_with_no_candidate_types_does_not_raise(self, tenancy, connection, contact):
        """``Trigger.objects.none()`` goes through the workspace-scoped manager
        and raises ``UnscopedQueryError``; ``route_events`` would swallow it and
        the event would silently stop routing."""
        from apps.channels.events import EventType
        from apps.flows.triggers.budget import InlineBudget
        from apps.flows.triggers.context import RoutingMode, build_context
        from apps.flows.triggers.hooks import Stage
        from apps.flows.triggers.safety import first_step_is_safe

        _identity(connection, contact)
        event = inbound(connection, kind=EventType.OPT_OUT, text="STOP")
        context = build_context(connection, event, InlineBudget.start(), mode=RoutingMode.INLINE)

        assert first_step_is_safe(context, Stage.TRIGGER) is True

    def test_it_judges_the_default_reply_flow_too(self, tenancy, connection, contact):
        """A default reply is one more flow this pass could start, so an unsafe
        one has to send the event to the worker."""
        from apps.flows.models import Trigger
        from apps.flows.triggers.budget import InlineBudget
        from apps.flows.triggers.context import RoutingMode, build_context
        from apps.flows.triggers.hooks import Stage
        from apps.flows.triggers.safety import first_step_is_safe

        delayed = _keyword_flow(tenancy.workspace, graph([node("a", "smart_delay", DELAY)]), name="Slow fallback")
        Trigger(flow=delayed, type=TriggerType.DEFAULT_REPLY, config_json={}).save()
        _identity(connection, contact)
        context = build_context(
            connection, inbound(connection, text="hi"), InlineBudget.start(), mode=RoutingMode.INLINE
        )

        assert first_step_is_safe(context, Stage.TRIGGER) is False

    def test_its_query_count_does_not_grow_with_the_candidates(self, tenancy, connection, contact):
        """The N+1 this replaced ran one published-version query per candidate,
        on the path SPEC §7.1 budgets at 1.5 seconds. Asserted as "one candidate
        costs the same as four" rather than against a table name, so the test
        keeps meaning what it says if the query is reshaped."""
        from django.db import connection as db
        from django.test.utils import CaptureQueriesContext

        from apps.flows.triggers.budget import InlineBudget
        from apps.flows.triggers.context import RoutingMode, build_context
        from apps.flows.triggers.safety import trigger_first_step_is_safe

        _identity(connection, contact)

        def cost() -> int:
            context = build_context(
                connection, inbound(connection, text="help"), InlineBudget.start(), mode=RoutingMode.INLINE
            )
            with CaptureQueriesContext(db) as captured:
                assert trigger_first_step_is_safe(context) is True
            return len(captured.captured_queries)

        _trigger(_keyword_flow(tenancy.workspace, graph([node("a", "send_message", SEND)]), name="One"))
        with_one = cost()

        for index in range(3):
            _trigger(_keyword_flow(tenancy.workspace, graph([node("a", "send_message", SEND)]), name=f"F{index}"))
        with_four = cost()

        assert with_four == with_one, f"{with_one} queries for one candidate, {with_four} for four"


@pytest.mark.django_db
class TestInlineExecution:
    def test_a_safe_single_send_flow_completes_in_request(self, tenancy, connection, contact):
        flow = _keyword_flow(tenancy.workspace, graph([node("a", "send_message", SEND)]))
        _trigger(flow)
        _identity(connection, contact)
        clear_slow_connection(connection)

        with routing_adapter(Platform.TELEGRAM) as adapter:
            route_events(connection, [inbound(connection, text="help")])

        assert len(adapter.sends) == 1
        assert not _actions(tenancy.workspace).exists()

    def test_mark_seen_and_send_typing_fire_first(self, tenancy, connection, contact):
        """SPEC §7.1 puts them first, and this issue is their first caller."""
        flow = _keyword_flow(tenancy.workspace, graph([node("a", "send_message", SEND)]))
        _trigger(flow)
        _identity(connection, contact)
        clear_slow_connection(connection)

        with routing_adapter(Platform.TELEGRAM) as adapter:
            route_events(connection, [inbound(connection, text="help")])

        assert [call for call, _ in adapter.courtesies] == ["mark_seen", "send_typing"]

    def test_courtesy_calls_are_skipped_where_the_platform_has_no_typing(self, tenancy, contact):
        sms = connection_for(tenancy.workspace, platform=Platform.SMS, external_id="+15550002")
        flow = _keyword_flow(tenancy.workspace, graph([node("a", "send_message", SEND)]))
        _trigger(flow)
        _identity(sms, contact, user="+15550999")
        clear_slow_connection(sms)

        with routing_adapter(Platform.SMS) as adapter:
            route_events(sms, [inbound(sms, text="help", user="+15550999")])

        assert adapter.courtesies == []

    def test_a_smart_delay_first_flow_always_enqueues(self, tenancy, connection, contact):
        """SPEC §7.1's synchronous-safe set does not include smart_delay."""
        flow = _keyword_flow(tenancy.workspace, graph([node("a", "smart_delay", DELAY)]))
        _trigger(flow)
        _identity(connection, contact)
        clear_slow_connection(connection)

        with routing_adapter(Platform.TELEGRAM) as adapter:
            route_events(connection, [inbound(connection, text="help")])

        assert adapter.sends == []
        assert _actions(tenancy.workspace).count() == 1
        assert not FlowExecution.objects.for_workspace(tenancy.workspace).exists()

    def test_an_exhausted_budget_hands_the_rest_of_a_batch_to_the_queue(self, tenancy, connection, contact):
        flow = _keyword_flow(tenancy.workspace, graph([node("a", "send_message", SEND)]))
        _trigger(flow)
        _identity(connection, contact)
        clear_slow_connection(connection)

        import apps.flows.triggers.pipeline as pipeline

        original = InlineBudget.start

        def spent(seconds=None):
            return InlineBudget(deadline=time.monotonic() - 1)

        InlineBudget.start = staticmethod(spent)
        try:
            with routing_adapter(Platform.TELEGRAM) as adapter:
                route_events(connection, [inbound(connection, text="help")])
        finally:
            InlineBudget.start = original

        assert adapter.sends == []
        assert _actions(tenancy.workspace).count() == 1
        assert _actions(tenancy.workspace).get().payload["stage"] == "resume"
        assert pipeline.ROUTABLE_EVENTS  # module still importable, no state left behind

    def test_a_deferred_event_is_only_enqueued_once(self, tenancy, connection, contact):
        """The idempotency key is built from ids this workspace owns, so a repeat
        returns the existing row instead of a second one."""
        flow = _keyword_flow(tenancy.workspace, graph([node("a", "smart_delay", DELAY)]))
        _trigger(flow)
        _identity(connection, contact)
        clear_slow_connection(connection)

        with routing_adapter(Platform.TELEGRAM):
            event = inbound(connection, text="help")
            route_events(connection, [event])
            route_events(connection, [event])

        assert _actions(tenancy.workspace).count() == 1


@pytest.mark.django_db(transaction=True)
class TestLockContention:
    def test_contention_falls_back_to_enqueue(self, tenancy):
        """SPEC §9.6: the web request must enqueue rather than block behind
        whatever the worker is doing to this contact."""
        from apps.queueing.locks import contact_lock

        connection = connection_for(tenancy.workspace, external_id="bot-lock")
        contact = contact_for(tenancy.workspace)
        flow = _keyword_flow(tenancy.workspace, graph([node("a", "send_message", SEND)]))
        _trigger(flow)
        _identity(connection, contact)
        clear_slow_connection(connection)

        held = threading.Event()
        release = threading.Event()

        def holder():
            try:
                with transaction.atomic(), contact_lock(contact):
                    held.set()
                    release.wait(timeout=15)
            finally:
                connections.close_all()

        thread = threading.Thread(target=holder)
        thread.start()
        try:
            assert held.wait(timeout=10)
            with routing_adapter(Platform.TELEGRAM) as adapter:
                route_events(connection, [inbound(connection, text="help")])

            assert adapter.sends == []
            assert _actions(tenancy.workspace).count() == 1
            assert _actions(tenancy.workspace).get().payload["stage"] == "resume"
        finally:
            release.set()
            thread.join(timeout=15)
            assert not thread.is_alive()
            ScheduledAction.objects.unscoped().filter(workspace=tenancy.workspace).delete()  # transaction=True
            FlowExecution.objects.unscoped().filter(contact=contact).delete()


@pytest.mark.django_db(transaction=True)
class TestAckLatency:
    def test_the_ack_stays_under_500ms_with_a_slow_adapter(self, tenancy):
        """SPEC §21's webhook-ack budget, against a platform that is genuinely slow.

        Routing cannot predict a slow ``send``; it can decline to start one. The
        courtesy calls are the probe — they hit the same API the send will — and
        one overrun flags the connection so every later event enqueues before
        doing any I/O at all.

        Measured the way apps/channels/tests/test_webhooks.py does: one warm-up
        (the request that pays and trips the breaker), then best-of-three, so
        the assertion is about the code path rather than CI's worst scheduling
        moment.
        """
        connection = connection_for(tenancy.workspace, external_id="bot-slow")
        contact = contact_for(tenancy.workspace)
        flow = _keyword_flow(tenancy.workspace, graph([node("a", "send_message", SEND)]))
        _trigger(flow)
        _identity(connection, contact)
        clear_slow_connection(connection)

        try:
            with routing_adapter(Platform.TELEGRAM, delay=1.0):
                route_events(connection, [inbound(connection, text="help", event_id="warmup")])
                assert connection_is_slow(connection), "the warm-up should have tripped the breaker"

                timings = []
                for index in range(3):
                    started = time.perf_counter()
                    route_events(connection, [inbound(connection, text="help", event_id=f"t{index}")])
                    timings.append(time.perf_counter() - started)

            assert min(timings) < 0.5, f"fastest of three was {min(timings):.3f}s"
        finally:
            clear_slow_connection(connection)
            cache.clear()
            ScheduledAction.objects.unscoped().filter(workspace=tenancy.workspace).delete()
            FlowExecution.objects.unscoped().filter(contact=contact).delete()
