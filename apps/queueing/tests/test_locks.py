"""Contact advisory locks (SPEC §9.6).

The one-step-per-contact invariant is only as good as these, so the tests here
prove contention across two real database connections rather than asserting
that a function was called.
"""

import threading
import uuid

import pytest
from django.db import connections, transaction

from apps.queueing.locks import (
    LockOutsideTransactionError,
    contact_lock,
    contact_lock_key,
    try_contact_lock,
)
from apps.queueing.tests.support import contact_lock_is_held


class TestLockKey:
    def test_the_key_is_the_documented_format(self) -> None:
        contact_id = uuid.UUID("0192f4b1-1c2d-7e3f-8a4b-5c6d7e8f9a0b")
        assert contact_lock_key(contact_id) == f"contact:{contact_id}"

    def test_a_string_and_a_uuid_produce_the_same_key(self) -> None:
        """Otherwise the worker and the engine would take *different* locks."""
        contact_id = uuid.uuid4()
        assert contact_lock_key(str(contact_id)) == contact_lock_key(contact_id)

    def test_case_does_not_split_the_lock(self) -> None:
        contact_id = uuid.uuid4()
        assert contact_lock_key(str(contact_id).upper()) == contact_lock_key(contact_id)

    def test_an_object_with_a_pk_is_accepted(self) -> None:
        contact_id = uuid.uuid4()

        class FakeContact:
            pk = contact_id

        assert contact_lock_key(FakeContact()) == contact_lock_key(contact_id)

    def test_none_is_refused(self) -> None:
        """A None id would collapse every caller onto one shared lock."""
        with pytest.raises(ValueError, match="needs a contact id"):
            contact_lock_key(None)


@pytest.mark.django_db(transaction=True)
class TestTransactionRequirement:
    """``transaction=True`` so the test itself is not wrapped in an atomic block.

    Under the ordinary ``django_db`` fixture every test *is* inside a
    transaction, which would make "outside a transaction" untestable — and
    would quietly make the guard look satisfied everywhere.
    """

    def test_contact_lock_refuses_to_run_outside_a_transaction(self) -> None:
        """The failure this guard exists for is silent, so the guard is loud."""
        assert not transaction.get_connection().in_atomic_block
        with pytest.raises(LockOutsideTransactionError), contact_lock(uuid.uuid4()):
            pass

    def test_try_contact_lock_refuses_too(self) -> None:
        with pytest.raises(LockOutsideTransactionError), try_contact_lock(uuid.uuid4()):
            pass

    def test_inside_a_transaction_it_works(self) -> None:
        contact_id = uuid.uuid4()
        with transaction.atomic(), contact_lock(contact_id):
            assert contact_lock_is_held(contact_id)

    def test_the_lock_is_re_entrant_within_one_transaction(self) -> None:
        """The worker takes it, then an engine node takes it again (SPEC §9.6)."""
        contact_id = uuid.uuid4()
        with transaction.atomic(), contact_lock(contact_id), contact_lock(contact_id):
            assert contact_lock_is_held(contact_id)

    def test_two_contacts_do_not_block_each_other(self) -> None:
        with transaction.atomic(), contact_lock(uuid.uuid4()), try_contact_lock(uuid.uuid4()) as acquired:
            assert acquired is True


@pytest.mark.django_db(transaction=True)
class TestContention:
    """Two connections, one contact. This is the invariant, proved."""

    def test_try_contact_lock_returns_false_while_another_session_holds_it(self) -> None:
        contact_id = uuid.uuid4()
        held = threading.Event()
        release = threading.Event()
        outcome: dict[str, bool] = {}

        def holder() -> None:
            try:
                with transaction.atomic(), contact_lock(contact_id):
                    held.set()
                    release.wait(timeout=10)
            finally:
                connections["default"].close()

        thread = threading.Thread(target=holder)
        thread.start()
        try:
            assert held.wait(timeout=10), "the holder thread never took the lock"
            with transaction.atomic(), try_contact_lock(contact_id) as acquired:
                outcome["while_held"] = acquired
        finally:
            release.set()
            thread.join(timeout=10)

        assert outcome["while_held"] is False

        # And free again once the holder's transaction committed: an xact lock
        # cannot be leaked by a process that forgot to release it.
        with transaction.atomic(), try_contact_lock(contact_id) as acquired:
            assert acquired is True

    def test_contact_lock_waits_for_the_other_session(self) -> None:
        """The worker's blocking path: it queues behind the holder, it does not skip."""
        contact_id = uuid.uuid4()
        held = threading.Event()
        release = threading.Event()
        waiter_got_lock = threading.Event()

        def holder() -> None:
            try:
                with transaction.atomic(), contact_lock(contact_id):
                    held.set()
                    release.wait(timeout=10)
            finally:
                connections["default"].close()

        def waiter() -> None:
            try:
                with transaction.atomic(), contact_lock(contact_id):
                    waiter_got_lock.set()
            finally:
                connections["default"].close()

        holder_thread = threading.Thread(target=holder)
        waiter_thread = threading.Thread(target=waiter)
        holder_thread.start()
        try:
            assert held.wait(timeout=10), "the holder thread never took the lock"
            waiter_thread.start()
            # Blocked, not refused: contact_lock has no non-blocking path.
            assert not waiter_got_lock.wait(timeout=0.5)
        finally:
            release.set()
            holder_thread.join(timeout=10)

        assert waiter_got_lock.wait(timeout=10), "the waiter never acquired the lock after release"
        waiter_thread.join(timeout=10)
