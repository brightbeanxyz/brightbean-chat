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


@pytest.fixture(autouse=True)
def _reset_active_language() -> Any:
    """End every test with no language activated.

    ``LanguagePreferenceMiddleware`` activates a signed-in user's language for
    the length of one request — see its own docstring — and Django's
    ``LocaleMiddleware`` never calls ``deactivate()`` afterward; a real server
    does not need it to, because the *next* request activates its own language
    regardless of what the last one left active. A test process has no such
    guarantee: a test that logs in as a Russian-preferring user and never runs
    another request leaves "ru" active for whatever test happens to run next
    in the same process, and that test can be one that compares translated
    text with no request — and therefore no re-activation — of its own.

    ``translation.override()`` does not need this: it always restores
    whatever was active before it on exit, request or no request. Only a test
    that reaches translation activation some other way — signing in as a user
    with a stored language and making a request being the one path this app
    has — needs the guard.

    Unconditional, unlike ``_isolate_cache``: deactivating a language touches
    no database, so there is nothing here that a non-``django_db`` test could
    fail on. An earlier version gated this on ``"db" in request.fixturenames``
    the same way ``_isolate_cache`` does, which was wrong in a way
    ``_isolate_cache`` isn't — pytest-django's ``django_db`` *marker* grants
    database access without ever adding the string ``"db"`` to
    ``fixturenames`` (only requesting the ``db``/``transactional_db`` fixture
    by name does that), so a marker-only test — ``test_language_middleware.py``
    requests just ``client`` — skipped the reset it most needed (Copilot
    review, PR #1).
    """
    yield
    from django.utils import translation

    translation.deactivate_all()


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
