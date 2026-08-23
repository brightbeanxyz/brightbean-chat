"""SPEC §17's rate limit: 10 requests per second per key.

Two claims, and the second one is the one that matters operationally: a burst
from one key is refused, and a *different* key is untouched by it. A limiter
keyed on anything coarser — the workspace, the client address — would let one
badly written integration take out every other one in the same account.
"""

import pytest

from apps.api import ratelimit
from apps.api.ratelimit import KEY_NAMESPACE, RATE_WINDOW_SECONDS, over_key_limit
from apps.api.tests.conftest import bearer, make_key
from apps.common.models import RateLimitCounter
from apps.common.ratelimit import window_key

CONTACTS = "/api/v1/contacts"

#: An arbitrary fixed instant. Any value works; what matters is that it does not
#: move.
FROZEN = 1_800_000_000.0


class Clock:
    """A window clock the test moves by hand.

    A fixed window is derived from the wall clock, so a burst that straddles a
    second boundary lands in two windows and the last request is allowed. That
    is correct behaviour and a flaky test — the suite runs with ``-n auto`` on
    machines under load, so "these five requests happen inside one second" is
    not something a test may assume. Freezing it makes the *limit* the thing
    under test rather than the scheduler.
    """

    def __init__(self, now: float = FROZEN) -> None:
        self.now = now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def window_key(self, namespace: str, identity: str, *, window_seconds: int, now: float | None = None) -> str:
        return window_key(namespace, identity, window_seconds=window_seconds, now=self.now if now is None else now)


@pytest.fixture
def clock(monkeypatch):
    frozen = Clock()
    monkeypatch.setattr(ratelimit, "window_key", frozen.window_key)
    return frozen


@pytest.mark.django_db
class TestPerKeyLimit:
    def test_a_burst_past_the_limit_is_refused_with_retry_after(self, client, tenancy, api_key, settings, clock):
        settings.API_RATE_LIMIT_PER_SECOND = 3
        _, plaintext = api_key

        codes = [client.get(CONTACTS, **bearer(plaintext)).status_code for _ in range(5)]

        assert codes[:3] == [200, 200, 200]
        assert codes[3:] == [429, 429]

    def test_the_429_carries_the_documented_body_and_header(self, client, tenancy, api_key, settings, clock):
        settings.API_RATE_LIMIT_PER_SECOND = 1
        _, plaintext = api_key
        client.get(CONTACTS, **bearer(plaintext))

        response = client.get(CONTACTS, **bearer(plaintext))

        assert response.status_code == 429
        assert response.json()["error"]["code"] == "rate_limited"
        # The window is one second wide, so this is the true wait rather than a
        # guess a caller has to pad.
        assert response["Retry-After"] == str(RATE_WINDOW_SECONDS)

    def test_independent_keys_do_not_contend(self, client, tenancy, settings, clock):
        settings.API_RATE_LIMIT_PER_SECOND = 2
        _, first = make_key(tenancy.workspace, name="first")
        _, second = make_key(tenancy.workspace, name="second")

        for _ in range(4):
            client.get(CONTACTS, **bearer(first))

        assert client.get(CONTACTS, **bearer(first)).status_code == 429
        assert client.get(CONTACTS, **bearer(second)).status_code == 200

    def test_the_counter_lives_in_postgres_not_the_cache(self, tenancy, api_key, settings, clock):
        """``apps.common.ratelimit``, not ``DatabaseCache.incr``.

        The cache's ``incr`` is get-then-set and loses counts under
        concurrency, which is the reason that module exists. Asserting on the
        counter row is how this test would notice a switch back.
        """
        settings.API_RATE_LIMIT_PER_SECOND = 5
        key, _ = api_key

        over_key_limit(key)

        assert RateLimitCounter.objects.filter(key__startswith=f"{KEY_NAMESPACE}:").exists()

    def test_a_new_window_forgives_the_burst(self, client, tenancy, api_key, settings, clock):
        """The window number is part of the counter key, so time moves the key.

        Nothing else carries a block forward — no ban row, no cooldown. A caller
        that waits out the ``Retry-After`` it was handed is served.
        """
        settings.API_RATE_LIMIT_PER_SECOND = 1
        _, plaintext = api_key
        client.get(CONTACTS, **bearer(plaintext))
        assert client.get(CONTACTS, **bearer(plaintext)).status_code == 429

        clock.advance(RATE_WINDOW_SECONDS)

        assert client.get(CONTACTS, **bearer(plaintext)).status_code == 200
