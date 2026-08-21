"""The Postgres-backed rate limiter (SPEC §22)."""

import threading

import pytest
from django.db import connection

from apps.common.models import RateLimitCounter
from apps.common.ratelimit import hit, window_key


@pytest.mark.django_db
class TestCounting:
    def test_the_nth_attempt_is_the_one_refused(self):
        key = "probe:1"

        results = [hit(key, limit=3, window_seconds=60) for _ in range(5)]

        assert results == [False, False, False, True, True]

    def test_separate_keys_have_separate_budgets(self):
        for _ in range(4):
            hit("probe:a", limit=3, window_seconds=60)

        assert hit("probe:b", limit=3, window_seconds=60) is False

    def test_the_count_is_stored(self):
        for _ in range(3):
            hit("probe:c", limit=10, window_seconds=60)

        assert RateLimitCounter.objects.get(key="probe:c").count == 3


class TestWindowKeys:
    def test_the_window_number_is_part_of_the_key(self):
        start = 60 * 16_666

        assert window_key("auth", "ip", window_seconds=60, now=start) == window_key(
            "auth", "ip", window_seconds=60, now=start + 59
        )
        assert window_key("auth", "ip", window_seconds=60, now=start) != window_key(
            "auth", "ip", window_seconds=60, now=start + 60
        )

    def test_namespaces_do_not_collide(self):
        assert window_key("auth", "ip", window_seconds=60) != window_key("api", "ip", window_seconds=60)

    def test_the_identity_is_not_stored_in_the_clear(self):
        assert "203.0.113.9" not in window_key("auth", "203.0.113.9", window_seconds=60)


@pytest.mark.django_db(transaction=True)
class TestConcurrentAttemptsAreNotLost:
    """The reason this is a row and not a cache entry.

    ``DatabaseCache`` inherits ``BaseCache.incr``, which is a get followed by a
    set, so two workers read the same count and one write lands on top of the
    other. Attempts are dropped rather than merely counted late, which is
    exactly the concurrency an attacker generates.
    """

    def test_every_attempt_is_counted(self):
        key = "probe:concurrent"
        workers = 12
        barrier = threading.Barrier(workers)

        def attempt():
            try:
                barrier.wait(timeout=10)
                hit(key, limit=1000, window_seconds=60)
            finally:
                connection.close()

        threads = [threading.Thread(target=attempt) for _ in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        assert RateLimitCounter.objects.get(key=key).count == workers


@pytest.mark.django_db
class TestPruning:
    def test_expired_counters_are_dropped_when_a_window_turns_over(self):
        from datetime import timedelta

        from django.utils import timezone

        RateLimitCounter.objects.create(key="probe:stale", count=99, expires_at=timezone.now() - timedelta(hours=1))

        hit("probe:fresh", limit=10, window_seconds=60)

        assert not RateLimitCounter.objects.filter(key="probe:stale").exists()

    def test_live_counters_survive(self):
        hit("probe:live", limit=10, window_seconds=60)

        hit("probe:other", limit=10, window_seconds=60)

        assert RateLimitCounter.objects.filter(key="probe:live").exists()
