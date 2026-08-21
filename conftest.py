"""Project-wide pytest fixtures.

Object builders live in ``tests/support.py`` so per-app test modules can import
them directly; the fixtures here are the common shapes.
"""

from typing import Any

import pytest
from django.test import Client

from tests.support import Tenancy, create_tenancy, create_user


@pytest.fixture(autouse=True)
def _isolate_cache(request: Any) -> Any:
    """Start every database-backed test with an empty cache.

    The auth rate limiter counts in the cache, and CACHE_URL defaults to the
    database cache — which, unlike LocMemCache, survives between tests in the
    same transaction-less run. Without this a test that logs in ten times leaves
    the next one pre-throttled.
    """
    if "db" not in request.fixturenames:
        return
    from django.core.cache import cache

    cache.clear()


#: What ``frozen_rate_limit_window`` pins the limiter's clock to. Arbitrary:
#: what matters is that it does not move, not where it stops.
FROZEN_RATE_LIMIT_CLOCK = 1_700_000_000.0


@pytest.fixture
def frozen_rate_limit_window(monkeypatch: Any) -> float:
    """Stop the rate limiter's clock, so a burst cannot straddle a window.

    ``apps.common.ratelimit.window_key`` puts the window number in the key —
    ``int(time.time() // window_seconds)`` — which is what makes the window
    *fixed* rather than sliding, and is deliberate: it is what makes the
    ``Retry-After`` the limiter hands out truthful. Read that module before
    reaching for a change there instead of here.

    The consequence lands on tests. One that fires ``limit + 1`` requests in a
    tight loop passes only while that sub-second burst stays inside one window;
    when it happens to cross a boundary the hits split across two counters —
    eight and three, say — neither trips the limit, and the assertion that the
    last request is refused fails with a bare ``assert 200 == 429``. It cost
    about one full-suite run in five before this fixture existed.

    Frozen rather than merely placed mid-window, because with no elapsed time
    there is no boundary to cross whatever the window size is — so this stays
    correct for issue #25's per-API-key limit and #4's signature-failure
    throttle, which use the same helpers with different windows.

    The stand-in replaces the *module's* ``time`` reference rather than patching
    the stdlib module in place: only ``apps.common.ratelimit`` sees a stopped
    clock, and Django, the test client and everything else keep the real one.
    """
    from apps.common import ratelimit

    class _StoppedClock:
        @staticmethod
        def time() -> float:
            return FROZEN_RATE_LIMIT_CLOCK

    monkeypatch.setattr(ratelimit, "time", _StoppedClock)
    return FROZEN_RATE_LIMIT_CLOCK


@pytest.fixture
def secret_value() -> str:
    """An opaque high-entropy secret with no recognisable credential shape.

    Deliberately shapeless. An earlier version of this fixture was a
    Telegram-style ``<bot_id>:<secret>`` token, which meant the log-scrubbing
    test passed on that one pattern alone — gutting every key-name rule left it
    green. A value only the surrounding ``token=`` / ``Bearer`` context can
    identify makes the test exercise the rule it claims to.
    """
    return "Zq4tPmXk9BvRnLwCyHsDfGjKaEuT7NbM2VxQ"


@pytest.fixture
def user(db: Any) -> Any:
    """A user with no organization.

    That is the whole point of dropping Studio's ``post_save`` provisioning:
    creating a user creates a user.
    """
    return create_user("solo@example.test")


@pytest.fixture
def tenancy(db: Any) -> Tenancy:
    """One organization, one workspace, and a user holding each of the four roles."""
    return create_tenancy("acme")


@pytest.fixture
def other_tenancy(db: Any) -> Tenancy:
    """A second, entirely separate tenant — the attacker's side of every IDOR test."""
    return create_tenancy("rival")


@pytest.fixture
def client_for(db: Any) -> Any:
    """``client_for(user)`` → a logged-in test client for that user."""

    def _client_for(target: Any) -> Client:
        client = Client()
        client.force_login(target)
        return client

    return _client_for
