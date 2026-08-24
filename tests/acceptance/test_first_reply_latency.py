"""SPEC §21 phase 1: "first automated reply p95 < 2 s on a 2 vCPU box".

The ack half of that sentence has been measured since Layer 4
(``apps/flows/tests/test_routing_inline.py::TestAckLatency``). The reply half
never was, and it is the harder one: the ack only has to decide, while the reply
has to match a trigger, take the contact lock, run the engine, pass compliance,
spend a rate-limit token and reach the adapter — all inside the webhook request.

**Why this asserts on the minimum and reports the rest.** The spec's number is a
p95 on a 2 vCPU box, which is a production SLO. A p95 over a handful of samples
on a shared CI runner is mostly a measurement of the runner: this repo has been
bitten three times by a test asserting on a clock-derived value (PRs #46, #49,
#62), and the standing lesson from #62 is to assert the invariant rather than the
timestamp. So:

* the invariant is asserted next door in ``test_first_reply.py`` — the inline
  budget fits inside the spec ceiling, so a reply that happens at all happens in
  time, or it is handed to the queue by construction;
* the wall clock is asserted here on **two** statistics, against the spec's own
  unmodified ceiling. ``min(timings)`` is the floor — the closest observable to
  what the code path actually costs, and what catches a regression that slows
  every reply. ``median(timings)`` is the typical case, and it is the one that
  catches a regression slowing *most* replies but not all: a run of
  ``[0.1, 3, 3, 3, 3]`` has a fast minimum and is plainly broken. An earlier
  version of this test asserted only the minimum and would have passed it;
* ``p95`` and ``max`` are **reported, not asserted**, and the reason is
  arithmetic rather than squeamishness: at these sample counts the nearest-rank
  p95 *is* the maximum, and the maximum is the one statistic that measures the
  runner rather than the code. The p95 the spec actually asks for needs more
  samples than a unit test should take, which is what the reference run is for
  (see this directory's README — it is a criterion row of its own, marked
  manual, so the gate does not quietly claim CI covers it);
* if the margin ever proves too tight on a loaded runner, **the lever is
  ``SAMPLES``, not the ceiling**. More samples can only lower a minimum and
  tighten a median, so raising it strengthens the test against noise, while a
  multiplier on the ceiling would weaken the thing being asserted. Do not add a
  CI multiplier here; there is none anywhere in this repo, deliberately;
* the distribution is reported in the failure message rather than asserted, so
  a human reading a red build sees the shape rather than one number;
* the p95 the spec actually names comes from a reference run on a 2 vCPU box,
  recorded in this directory's README.

That is the same shape ``TestAckLatency`` and
``apps/channels/tests/test_webhooks.py`` already use, for the same reasons.

The measured path is the real one: a signed delivery to the real webhook URL,
through the real endpoint, the real dispatch seam, the real persistence and
routing processors, the real engine and the real messaging facade. Only the
platform's HTTP is replaced, by a fake that is itself a real ``Adapter``
registered through the real registry.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest
from django.core.cache import cache
from django.test import Client

from apps.channels.models import ChannelConnection
from apps.channels.tests.fake_adapter import SECRET_HEADER, SIGNATURE_HEADER, sign
from apps.common.platforms import Platform
from apps.flows.models import FlowExecution, Trigger, TriggerType
from apps.flows.tests.routing_support import routing_adapter
from apps.flows.tests.support import graph, node, published_flow
from apps.flows.triggers.budget import clear_slow_connection
from apps.flows.triggers.handlers import ROUTE_EVENT
from apps.messaging.models import Message, MessageDirection, MessageStatus
from apps.queueing.models import ScheduledAction
from tests.acceptance.criteria import spec_latency_budgets

WEBHOOK_URL = "/webhooks/telegram/"
SEND = {"blocks": [{"type": "text", "text": "Thanks — someone will be with you shortly."}]}

#: Seven, not three. Three is enough for a minimum but leaves the median resting
#: on two samples, and the median is half of what is asserted below — at seven it
#: takes four slow replies to trip, which is a regression rather than a hiccup.
#: Each sample costs well under a tenth of a second on this path.
SAMPLES = 7


def percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank, on a sorted copy. No numpy, and no interpolation to argue about."""
    if not values:
        # ``min(len - 1, ...)`` is -1 here, which would index the last element of
        # an empty list and raise IndexError from inside a latency assertion.
        # Say what actually went wrong instead.
        raise ValueError("percentile() needs at least one sample; none were collected")
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * len(ordered)) - 1))
    return ordered[index]


def deliver(client: Client, secret: str, event_id: str, *, text: str = "help") -> Any:
    body = json.dumps({"events": [{"id": event_id, "user": "tg-acceptance", "text": text}]}).encode()
    return client.post(
        WEBHOOK_URL,
        data=body,
        content_type="application/json",
        headers={SECRET_HEADER: secret, SIGNATURE_HEADER: sign(secret, body)},
    )


@pytest.mark.django_db(transaction=True)
class TestFirstReplyLatency:
    """One class, one test: the criterion is a single measured scenario."""

    def test_the_first_automated_reply_lands_inside_the_spec_budget(self, client: Client, tenancy: Any) -> None:
        """Warm up once, then time five deliveries end to end.

        ``transaction=True`` rather than the default, and not for the threads —
        there are none. The send pipeline opens its own transactions and the
        rate-limit bucket reads Postgres' ``now()``, which inside a wrapping
        test transaction is frozen at the transaction's start. Measuring there
        would flatter the number by skipping every real commit on the path, and
        it is the commits that a 2 vCPU box notices.

        The cost is that nothing rolls back, so the teardown below is
        load-bearing rather than tidy.
        """
        ceiling = spec_latency_budgets()["first_reply"]

        connection = ChannelConnection(
            workspace=tenancy.workspace,
            platform=Platform.TELEGRAM,
            display_name="Support bot",
            external_id="bot-acceptance",
        )
        connection.rotate_webhook_secret()
        connection.save()
        secret = connection.webhook_secret

        flow = published_flow(tenancy.workspace, graph([node("a", "send_message", SEND)]), name="First reply")
        Trigger(
            flow=flow,
            type=TriggerType.KEYWORD,
            config_json={"keywords": [{"text": "help", "mode": "contains"}]},
        ).save()

        # No send-bucket setup, deliberately. Telegram's default rate is 25/s and
        # this sends six messages; the bucket only bites on SMS, which is 1/s.
        # An unexplained bucket poke here would look like part of the measurement.
        clear_slow_connection(connection)

        try:
            with routing_adapter(Platform.TELEGRAM) as adapter:
                # The warm-up pays for the connection pool, the template cache
                # and the first query plan, exactly as TestAckLatency's does.
                assert deliver(client, secret, "warmup").status_code == 200
                assert len(adapter.sends) == 1, "the warm-up must actually produce a reply, or nothing below is timed"

                timings: list[float] = []
                for index in range(SAMPLES):
                    started = time.perf_counter()
                    response = deliver(client, secret, f"evt-{index}")
                    timings.append(time.perf_counter() - started)
                    assert response.status_code == 200

            replies = len(adapter.sends)
            fastest, typical = min(timings), percentile(timings, 0.5)
            p95, slowest = percentile(timings, 0.95), max(timings)
            shape = f"min={fastest:.3f}s p50={typical:.3f}s p95={p95:.3f}s max={slowest:.3f}s"

            # A latency test that would pass while sending nothing is the
            # classic way this assertion goes quietly vacuous.
            assert replies == SAMPLES + 1, f"expected one reply per delivery, got {replies}"
            sent = Message.objects.for_workspace(tenancy.workspace).filter(direction=MessageDirection.OUT)
            assert sent.count() == SAMPLES + 1
            assert set(sent.values_list("status", flat=True)) == {MessageStatus.SENT}

            # And every one of them ran in the request. This is the non-clock
            # regression detector: a reply handed to the worker is precisely
            # what makes a first reply slow, and it is visible without a stopwatch.
            deferred = ScheduledAction.objects.for_workspace(tenancy.workspace).filter(type=ROUTE_EVENT)
            assert not deferred.exists(), (
                f"{deferred.count()} deliveries were handed to the queue instead of answered inline "
                f"(reasons: {sorted(action.payload.get('reason', '?') for action in deferred)}). "
                f"SPEC §7.1 runs a safe first step in the request; the p95 above only describes that path."
            )

            # The floor: every reply is slow, so the path itself regressed.
            assert fastest < ceiling, (
                f"the fastest of {SAMPLES} first replies was over SPEC §21's {ceiling}s ceiling ({shape}). "
                f"Not one reply made the budget, so this is the reply path rather than a busy runner. "
                f"Reference numbers for a 2 vCPU box are in tests/acceptance/README.md."
            )
            # The typical case: most replies are slow while some are not. A
            # minimum cannot see this, which is why it is not the only assertion.
            assert typical < ceiling, (
                f"the median of {SAMPLES} first replies was over SPEC §21's {ceiling}s ceiling ({shape}). "
                f"More than half the replies missed the budget while at least one made it, which is the "
                f"shape of a regression that only bites some of the time — contention on the contact "
                f"lock, a cache that helps only the first request, a retry that fires intermittently. "
                f"Reference numbers for a 2 vCPU box are in tests/acceptance/README.md."
            )
        finally:
            # transaction=True means none of this rolls back, and the slow-connection
            # flag lives in the cache with a 60-second TTL — a leaked one fails an
            # unrelated test later, which is the worst kind of flake to trace.
            clear_slow_connection(connection)
            cache.clear()
            ScheduledAction.objects.for_workspace(tenancy.workspace).delete()
            FlowExecution.objects.for_workspace(tenancy.workspace).delete()


class TestTheStatisticsCatchWhatTheyClaimTo:
    """The assertions above rest on these two statistics. Prove they discriminate.

    No database and no clock: the point is the arithmetic, and the arithmetic is
    what decides whether a red build means anything.
    """

    CEILING = 2.0

    def test_the_median_catches_a_run_that_is_mostly_slow(self) -> None:
        """The case a minimum alone cannot see, and the reason it is not alone.

        One fast reply among four slow ones is a system missing its budget most
        of the time. ``min`` says 0.1 s and waves it through.
        """
        mostly_slow = [0.1, 3.0, 3.0, 3.0, 3.0]
        assert min(mostly_slow) < self.CEILING, "the minimum is exactly what fails to notice this"
        assert not percentile(mostly_slow, 0.5) < self.CEILING

    def test_the_median_tolerates_a_single_outlier(self) -> None:
        """And the reason the maximum is reported rather than asserted.

        One sample ten times the others is a runner scheduling hiccup, not a
        regression. An assertion on the max would fail here; the median does not.
        """
        healthy_with_a_spike = [0.05, 0.06, 0.06, 0.07, 0.08, 0.09, 0.9]
        assert percentile(healthy_with_a_spike, 0.5) < self.CEILING
        assert max(healthy_with_a_spike) > 10 * percentile(healthy_with_a_spike, 0.5)

    def test_the_minimum_catches_a_run_that_is_uniformly_slow(self) -> None:
        """The half the median would also catch, kept because it names the cause.

        Nothing made the budget, so the failure message can say the path
        regressed rather than hedging about the runner.
        """
        all_slow = [2.5, 2.6, 2.7, 3.0]
        assert not min(all_slow) < self.CEILING

    def test_the_percentile_helper_stays_inside_the_sample(self) -> None:
        """Nearest-rank, so every answer is a sample that was actually measured."""
        samples = [0.1, 0.2, 0.3, 0.4, 0.5]
        for fraction in (0.0, 0.5, 0.95, 1.0):
            assert percentile(samples, fraction) in samples
        assert percentile(samples, 0.95) == max(samples), (
            "at these sample counts the nearest-rank p95 is the maximum, which is why it is reported "
            "rather than asserted — see this module's docstring"
        )
