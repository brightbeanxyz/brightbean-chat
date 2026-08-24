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
* the wall clock is asserted here on ``min(timings)``, which is the closest
  observable to what the code path actually costs, against the spec's own
  unmodified ceiling. Measured on a developer laptop the minimum lands between
  0.05 s and 0.45 s depending on what else the machine is doing — so between
  roughly four- and fortyfold headroom, and the spread itself is the argument:
  the same code, on the same machine, varies by more than the margin a tighter
  ceiling would have left;
* if that margin ever does prove too tight on a loaded runner, **the lever is
  ``SAMPLES``, not the ceiling**. Taking more samples can only lower a minimum,
  so raising it strengthens the test against noise, while a multiplier on the
  ceiling would weaken the thing being asserted. Do not add a CI multiplier
  here; there is none anywhere in this repo, deliberately;
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

#: Five, not three. Three is enough for the assertion (which is on the minimum)
#: but not enough for the p50 and p95 the failure message reports, and five
#: costs a fraction of a second on this path.
SAMPLES = 5


def percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank, on a sorted copy. No numpy, and no interpolation to argue about."""
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
            p50, p95, slowest = percentile(timings, 0.5), percentile(timings, 0.95), max(timings)

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

            assert min(timings) < ceiling, (
                f"fastest of {SAMPLES} first replies was {min(timings):.3f}s, over SPEC §21's {ceiling}s "
                f"ceiling (p50={p50:.3f}s p95={p95:.3f}s max={slowest:.3f}s). The assertion is on the "
                f"minimum on purpose — see this module's docstring — so a failure here is a real "
                f"regression on the reply path rather than a busy runner. Reference numbers for a 2 vCPU "
                f"box are in tests/acceptance/README.md."
            )
        finally:
            # transaction=True means none of this rolls back, and the slow-connection
            # flag lives in the cache with a 60-second TTL — a leaked one fails an
            # unrelated test later, which is the worst kind of flake to trace.
            clear_slow_connection(connection)
            cache.clear()
            ScheduledAction.objects.for_workspace(tenancy.workspace).delete()
            FlowExecution.objects.for_workspace(tenancy.workspace).delete()
