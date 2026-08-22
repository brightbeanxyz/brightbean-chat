"""The Postgres token bucket (SPEC §8's rate throttling)."""

import threading
import time
from datetime import timedelta
from typing import Any

import pytest
from django.db import connection as db_connection
from django.test import override_settings
from django.utils import timezone

from apps.channels.policy import policy_for
from apps.common.platforms import Platform
from apps.messaging import buckets
from apps.messaging.models import SendBucket
from apps.messaging.tests.conftest import make_connection

pytestmark = pytest.mark.django_db


def bucket_for(connection: Any) -> SendBucket:
    return SendBucket.objects.get(connection=connection)


class TestRates:
    def test_the_default_is_the_platform_policy(self) -> None:
        for platform in Platform.values:
            assert buckets.rate_for(platform) == policy_for(platform).rate_default

    @override_settings(DEFAULT_SEND_RATE_OVERRIDES={"telegram": 3.5})
    def test_an_override_wins(self) -> None:
        """SPEC §20's env var. Meta hands limits out per app and per page, so a
        self-hoster's ceiling is not the published one."""
        assert buckets.rate_for(Platform.TELEGRAM) == 3.5
        assert buckets.rate_for(Platform.SMS) == policy_for(Platform.SMS).rate_default

    @override_settings(DEFAULT_SEND_RATE_OVERRIDES={"telegram": 5}, SEND_BUCKET_BURST_SECONDS=1.0)
    def test_a_changed_rate_reaches_an_existing_row(self, tenancy: Any) -> None:
        """No migration, no management command, no stale row to notice."""
        connection = make_connection(tenancy.workspace, suffix="rate")
        buckets.try_acquire(connection)
        assert bucket_for(connection).refill_rate == 5.0

        with override_settings(DEFAULT_SEND_RATE_OVERRIDES={"telegram": 9}):
            buckets.try_acquire(connection)
        assert bucket_for(connection).refill_rate == 9.0

    def test_capacity_is_never_below_one_token(self) -> None:
        """A platform configured below 1/s could otherwise never accumulate
        enough to send at all."""
        with override_settings(SEND_BUCKET_BURST_SECONDS=1.0):
            assert buckets.capacity_for(0.5) == 1.0


class TestAcquisition:
    @override_settings(DEFAULT_SEND_RATE_OVERRIDES={"telegram": 5}, SEND_BUCKET_BURST_SECONDS=1.0)
    def test_a_new_bucket_starts_full(self, tenancy: Any) -> None:
        """So a connection's first message does not wait for a bucket nobody
        has drained."""
        connection = make_connection(tenancy.workspace, suffix="new")
        assert isinstance(buckets.try_acquire(connection), buckets.Granted)
        assert bucket_for(connection).tokens == pytest.approx(4.0, abs=0.2)

    @override_settings(DEFAULT_SEND_RATE_OVERRIDES={"telegram": 1}, SEND_BUCKET_BURST_SECONDS=1.0)
    def test_an_empty_bucket_defers_with_the_wait(self, tenancy: Any) -> None:
        connection = make_connection(tenancy.workspace, suffix="empty")
        assert isinstance(buckets.try_acquire(connection), buckets.Granted)
        outcome = buckets.try_acquire(connection)
        assert isinstance(outcome, buckets.Deferred)
        assert 0 < outcome.wait_seconds <= 1.0

    @override_settings(DEFAULT_SEND_RATE_OVERRIDES={"telegram": 10}, SEND_BUCKET_BURST_SECONDS=1.0)
    def test_it_refills_over_time(self, tenancy: Any) -> None:
        connection = make_connection(tenancy.workspace, suffix="refill")
        buckets.try_acquire(connection)
        SendBucket.objects.filter(connection=connection).update(
            tokens=0.0, refilled_at=timezone.now() - timedelta(seconds=0.5)
        )
        assert isinstance(buckets.try_acquire(connection), buckets.Granted)

    @override_settings(DEFAULT_SEND_RATE_OVERRIDES={"telegram": 5}, SEND_BUCKET_BURST_SECONDS=1.0)
    def test_it_never_refills_past_capacity(self, tenancy: Any) -> None:
        """A connection idle overnight must not be able to send a night's worth
        of messages in one second."""
        connection = make_connection(tenancy.workspace, suffix="cap")
        buckets.try_acquire(connection)
        SendBucket.objects.filter(connection=connection).update(refilled_at=timezone.now() - timedelta(hours=8))
        buckets.try_acquire(connection)
        assert bucket_for(connection).tokens <= 5.0

    @override_settings(DEFAULT_SEND_RATE_OVERRIDES={"telegram": 1}, SEND_BUCKET_BURST_SECONDS=1.0)
    def test_a_zero_wait_budget_degrades_to_non_blocking(self, tenancy: Any) -> None:
        connection = make_connection(tenancy.workspace, suffix="nowait")
        buckets.try_acquire(connection)
        started = time.monotonic()
        assert isinstance(buckets.acquire(connection, max_wait=0), buckets.Deferred)
        assert time.monotonic() - started < 0.2

    @override_settings(DEFAULT_SEND_RATE_OVERRIDES={"telegram": 4}, SEND_BUCKET_BURST_SECONDS=0.1)
    def test_a_blocking_acquire_waits_for_a_token(self, tenancy: Any) -> None:
        """The starting state is forced rather than drained by calling.

        Draining with a first ``try_acquire`` and then asserting the second one
        defers races the refill: at any useful rate a token accrues in the
        milliseconds between two statements, and the test would pass or fail
        depending on how loaded the machine is.
        """
        connection = make_connection(tenancy.workspace, suffix="blocking")
        buckets.try_acquire(connection)
        SendBucket.objects.filter(connection=connection).update(tokens=0.0, refilled_at=timezone.now())

        # 4/s, so a token is due in 250ms — comfortably inside the budget.
        started = time.monotonic()
        assert isinstance(buckets.acquire(connection, max_wait=5.0), buckets.Granted)
        assert time.monotonic() - started >= 0.1

    @override_settings(DEFAULT_SEND_RATE_OVERRIDES={"telegram": 0.02}, SEND_BUCKET_BURST_SECONDS=1.0)
    def test_a_blocking_acquire_gives_up_rather_than_holding_a_worker(self, tenancy: Any) -> None:
        """Past the bound it hands the wait to the queue: a worker sleeping in
        its handler holds a transaction, a contact lock and a worker slot."""
        connection = make_connection(tenancy.workspace, suffix="giveup")
        buckets.try_acquire(connection)
        started = time.monotonic()
        assert isinstance(buckets.acquire(connection, max_wait=0.2), buckets.Deferred)
        assert time.monotonic() - started < 1.0


class TestTheClock:
    @override_settings(DEFAULT_SEND_RATE_OVERRIDES={"telegram": 5}, SEND_BUCKET_BURST_SECONDS=1.0)
    def test_refilled_at_comes_from_postgres(self, tenancy: Any) -> None:
        """Not the Python clock: two workers on two hosts have two clocks, and
        one running fast writes a refilled_at in the future that freezes the
        bucket for everybody until real time catches up."""
        connection = make_connection(tenancy.workspace, suffix="clock")
        before = timezone.now()
        buckets.try_acquire(connection)
        after = timezone.now()
        assert before <= bucket_for(connection).refilled_at <= after


@pytest.mark.django_db(transaction=True)
class TestUnderConcurrency:
    """The acceptance criterion: two workers, one connection, rate 5/s → ≤5/sec.

    Real threads and real connections, the shape ``apps/queueing/tests/
    test_concurrency.py`` and ``apps/common/tests/test_ratelimit.py`` both use.
    ``transaction=True`` because that is what gives each thread a connection
    that can commit — the ordinary fixture wraps everything in one transaction
    no other thread can see into.
    """

    RATE = 5.0
    BURST = 1.0
    SENDS = 16

    def _drain(self, connection: Any, grants: list[float], lock: threading.Lock, stop: float) -> None:
        try:
            while time.monotonic() < stop:
                outcome = buckets.try_acquire(connection)
                if isinstance(outcome, buckets.Granted):
                    with lock:
                        if len(grants) >= self.SENDS:
                            return
                        grants.append(time.monotonic())
                else:
                    time.sleep(min(outcome.wait_seconds, 0.05))
        finally:
            db_connection.close()

    def test_two_workers_never_exceed_the_rate(self, tenancy: Any) -> None:
        connection = make_connection(tenancy.workspace, suffix="concurrent")
        grants: list[float] = []
        lock = threading.Lock()
        deadline = time.monotonic() + 20.0

        with override_settings(
            DEFAULT_SEND_RATE_OVERRIDES={Platform.TELEGRAM.value: self.RATE},
            SEND_BUCKET_BURST_SECONDS=self.BURST,
        ):
            # Start empty, so the initial burst does not mask the pacing.
            buckets.try_acquire(connection)
            SendBucket.objects.filter(connection=connection).update(tokens=0.0)

            threads = [
                threading.Thread(target=self._drain, args=(connection, grants, lock, deadline), name=f"w{n}")
                for n in range(2)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
                assert not thread.is_alive(), f"{thread.name} did not finish"

        assert len(grants) == self.SENDS

        # The whole run took at least as long as the rate demands. Without the
        # bucket this is ~0 seconds, so the assertion fails loudly rather than
        # flakily if the throttle is ever removed.
        span = grants[-1] - grants[0]
        assert span >= (self.SENDS - self.RATE * self.BURST - 1) / self.RATE

        # And no one-second window holds more than the rate plus the configured
        # burst — the burst is real and is what SEND_BUCKET_BURST_SECONDS buys.
        ordered = sorted(grants)
        ceiling = self.RATE + self.RATE * self.BURST
        for index, start in enumerate(ordered):
            in_window = sum(1 for stamp in ordered[index:] if stamp - start < 1.0)
            assert in_window <= ceiling, f"{in_window} grants inside one second"

    def test_a_second_connection_has_its_own_bucket(self, tenancy: Any) -> None:
        """Per channel_connection, so one throttled page cannot starve another."""
        first = make_connection(tenancy.workspace, suffix="bucket-a")
        second = make_connection(tenancy.workspace, platform=Platform.SMS, suffix="bucket-b")
        with override_settings(DEFAULT_SEND_RATE_OVERRIDES={"telegram": 1, "sms": 1}):
            buckets.try_acquire(first)
            assert isinstance(buckets.try_acquire(first), buckets.Deferred)
            assert isinstance(buckets.try_acquire(second), buckets.Granted)
