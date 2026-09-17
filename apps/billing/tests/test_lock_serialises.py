"""``organization_locked`` actually serialises, proved by racing two callers.

The previous attempt at this did not. The helper opened its own transaction, so
at every call site that was not already inside one the lock was taken and
dropped *inside* the ``with`` block that held only the count — and the mutation
that followed was unlocked. Eight of nine sites were like that, and every test
passed, because none of them ran two callers at once.

So this one does, against a real database, with real threads. It is the shape
``apps/media_library/tests/test_quota_concurrency.py`` uses for the same
property one tier down.
"""

import threading
from typing import Any

import pytest
from django.db import connection, transaction

from apps.billing.entitlements import organization_locked
from apps.billing.tests.stripe_support import SECRET_KEY

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def _billing_on(settings: Any) -> None:
    settings.STRIPE_ENABLED = True
    settings.STRIPE_SECRET_KEY = SECRET_KEY
    settings.STRIPE_PRICE_ID_MONTHLY = "price_m"
    settings.STRIPE_PRICE_ID_YEARLY = "price_y"


class TestTheLockIsHeldAcrossTheBlock:
    def test_two_threads_cannot_be_inside_it_at_once(self, tenancy: Any) -> None:
        """The property the whole helper exists for.

        Each thread records when it entered and left. If the lock works the two
        intervals cannot overlap; if it is released early — the bug this file
        was written for — they will.
        """
        organization = tenancy.organization
        events: list[tuple[str, int]] = []
        guard = threading.Lock()
        step = iter(range(1000))

        def tick(label: str) -> None:
            with guard:
                events.append((label, next(step)))

        def worker(name: str) -> None:
            try:
                with organization_locked(organization):
                    tick(f"{name}:in")
                    # Long enough that a lock released early would let the other
                    # thread interleave here.
                    threading.Event().wait(0.3)
                    tick(f"{name}:out")
            finally:
                connection.close()

        threads = [threading.Thread(target=worker, args=(name,)) for name in ("a", "b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        ordered = [label for label, _ in sorted(events, key=lambda pair: pair[1])]

        assert len(ordered) == 4, f"both workers should have run; saw {ordered}"
        first = ordered[0].split(":")[0]
        assert ordered[1] == f"{first}:out", (
            f"the two critical sections interleaved: {ordered}. The lock is not held across the block."
        )

    def test_it_takes_no_lock_when_the_plan_cannot_refuse(self, tenancy: Any, settings: Any) -> None:
        """The guards short-circuit on an unlimited plan, so locking before
        reading the limit made every gated write on a self-hosted deployment
        queue behind one row for a check that can never say no."""
        settings.STRIPE_ENABLED = False
        organization = tenancy.organization
        entered = threading.Event()
        released = threading.Event()

        def holder() -> None:
            try:
                with organization_locked(organization):
                    entered.set()
                    released.wait(timeout=10)
            finally:
                connection.close()

        thread = threading.Thread(target=holder)
        thread.start()
        try:
            assert entered.wait(timeout=10)
            # With no lock taken, this must not block behind the holder.
            with organization_locked(organization):
                pass
        finally:
            released.set()
            thread.join(timeout=10)

    def test_the_block_is_atomic_whichever_plan_it_is(self, tenancy: Any, settings: Any) -> None:
        """The transaction is opened either way, so a caller's writes do not
        change atomicity depending on what the organization pays."""
        for enabled in (True, False):
            settings.STRIPE_ENABLED = enabled
            with organization_locked(tenancy.organization):
                assert transaction.get_connection().in_atomic_block is True
