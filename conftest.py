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
