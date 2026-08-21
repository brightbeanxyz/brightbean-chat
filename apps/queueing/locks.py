"""Contact advisory locks — the one-step-per-contact invariant (SPEC §9.6).

Every path that advances a contact's state takes the same lock:

    All execution (inline and worker) wraps in
    ``pg_advisory_xact_lock(hashtext('contact:' || contact_id))``.

These two context managers are the only lock primitives in BrightBean Chat. The
flow engine (#9) and the inline webhook path (#11) both call them; nothing else
should ever hand-roll a ``pg_advisory_*`` call, because the invariant only holds
while every participant computes the *same* key.

Why *transaction*-scoped locks rather than session-scoped ones: an xact lock is
released by COMMIT or ROLLBACK, including the rollback Django performs when a
handler raises. A session lock leaked by a crashed worker would wedge that
contact until the connection died, which on a pooled connection can be a very
long time. The cost is the rule below.

**They must be called inside a transaction.** Postgres runs a bare statement in
its own implicit transaction, so ``pg_advisory_xact_lock`` outside an explicit
block acquires the lock and releases it again before the next statement runs —
no error, no warning, and no mutual exclusion whatsoever. That silent
uselessness is worth a hard failure, so both helpers refuse to run outside
``transaction.atomic()``.

Advisory locks are *counted per session*, so taking the same lock twice inside
one transaction succeeds and costs nothing. The worker takes the lock before
dispatching (SPEC §9.6: claim, then lock, then touch the execution) and an
engine node re-taking it is therefore harmless — which is what lets the engine
call ``contact_lock`` unconditionally without knowing who called it.

Blocking versus non-blocking is the difference between the two entry points:

* ``contact_lock`` waits. The worker can afford to wait — it has no client on
  the other end of the socket.
* ``try_contact_lock`` returns immediately with a bool. The inline path uses it:
  a web request that cannot get the lock enqueues a ``resume_execution`` action
  instead of holding a gunicorn thread hostage (SPEC §9.6).
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from django.db import connection, transaction

from apps.queueing.models import coerce_contact_id

__all__ = [
    "LockOutsideTransactionError",
    "advisory_lock",
    "contact_lock",
    "contact_lock_key",
    "try_advisory_lock",
    "try_contact_lock",
]

logger = logging.getLogger(__name__)


class LockOutsideTransactionError(RuntimeError):
    """Raised when an xact-scoped advisory lock is requested outside a transaction."""


def contact_lock_key(contact: Any) -> str:
    """The lock key SPEC §9.6 names: ``contact:<uuid>``.

    Exported because the string is a cross-layer contract, not an
    implementation detail. Anything that needs the same lock — including SQL
    that builds the key itself — must produce exactly this.

    Normalisation is ``apps.queueing.models.coerce_contact_id``, the same
    function that decides what goes into the ``contact_id`` column. The key is
    hashed, so ``"AB-…"`` and ``"ab-…"`` would be *different* locks; sharing one
    normaliser with the writer is what stops the worker locking a key nobody
    else computes.
    """
    value = coerce_contact_id(contact)
    if value is None:
        raise ValueError("contact_lock() needs a contact id; None would lock a key shared by every caller.")
    return f"contact:{value}"


def _require_transaction(name: str) -> None:
    if transaction.get_connection().in_atomic_block:
        return
    raise LockOutsideTransactionError(
        f"{name}() was called outside a transaction. pg_advisory_xact_lock is released at the end "
        f"of the enclosing transaction, and a bare statement gets an implicit one — so the lock "
        f"would be taken and dropped before the work it protects even starts, with no error. "
        f"Wrap the call in transaction.atomic(). See docs/SPEC.md §9.6."
    )


@contextmanager
def advisory_lock(key: str, *, _caller: str = "advisory_lock") -> Iterator[None]:
    """Block until the transaction holds the advisory lock named by ``key``.

    The generic primitive. Contacts are the main consumer and get their own
    wrapper below, but the queue also serialises its housekeeping bootstrap
    this way — and both go through here so there is exactly one place that
    knows how a key becomes a lock, and one place to change if that ever needs
    a ``lock_timeout`` or a two-integer keyspace.
    """
    _require_transaction(_caller)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", [key])
    logger.debug("Acquired advisory lock %s", key)
    yield


@contextmanager
def try_advisory_lock(key: str, *, _caller: str = "try_advisory_lock") -> Iterator[bool]:
    """Take the advisory lock named by ``key`` if free; yield whether it was taken."""
    _require_transaction(_caller)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_xact_lock(hashtext(%s))", [key])
        row = cursor.fetchone()
    acquired = bool(row and row[0])
    logger.debug("Advisory lock %s %s", key, "acquired" if acquired else "busy")
    yield acquired


@contextmanager
def contact_lock(contact: Any) -> Iterator[None]:
    """Block until this contact's advisory lock is held; hold it until COMMIT.

    Usage::

        with transaction.atomic(), contact_lock(contact_id):
            ...advance the execution...
    """
    with advisory_lock(contact_lock_key(contact), _caller="contact_lock"):
        yield


@contextmanager
def try_contact_lock(contact: Any) -> Iterator[bool]:
    """Take this contact's lock if it is free; yield whether it was taken.

    Never blocks. The caller **must** check the yielded value — a ``False`` and
    a ``True`` look identical from the outside, so ignoring it is a race that
    only shows up under load::

        with transaction.atomic(), try_contact_lock(contact_id) as acquired:
            if not acquired:
                schedule(ActionType.RESUME_EXECUTION, ...)
                return
            ...advance the execution...
    """
    with try_advisory_lock(contact_lock_key(contact), _caller="try_contact_lock") as acquired:
        yield acquired
