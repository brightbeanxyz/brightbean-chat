"""The handler registry and the enqueue helper — the queue's public API.

Everything above Layer 2 that wants deferred work touches exactly two names
from this module: :func:`register_handler` to say what a type *does*, and
:func:`schedule` to put a row in the table. Neither requires editing this app,
which is the point — the queue is substrate, and the features that ride on it
arrive later and independently.

**How a later layer registers.** Put the handlers in your own app's
``handlers.py`` and import it from your ``AppConfig.ready()`` so registration is
an import side effect::

    # apps/flows/handlers.py
    from apps.queueing.registry import register_handler

    @register_handler("resume_execution")
    def resume_execution(payload, action):
        ...

    # apps/flows/apps.py
    class FlowsConfig(AppConfig):
        def ready(self):
            from apps.flows import handlers  # noqa: F401

Never add a branch to this module for your type, and never edit another app's
handler: the registry is additive by construction, and a duplicate registration
raises rather than silently replacing (two apps quietly claiming one type is a
bug that would otherwise surface as work running under the wrong code).

**The handler contract.** ``handler(payload, action) -> None``, called inside a
transaction that already holds the contact advisory lock when the row names a
contact (SPEC §9.6). Raise to retry: the worker rolls the transaction back and
reschedules with the SPEC §15 backoff. Return normally to mark the row done.
A handler must be safe to run more than once — a worker can die between the
handler committing and the row being marked, and zombie recovery will re-run it.
"""

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.queueing.models import DEFAULT_MAX_ATTEMPTS, ActionStatus, ScheduledAction, coerce_contact_id

__all__ = [
    "DuplicateHandlerError",
    "Handler",
    "IdempotencyKeyConflictError",
    "UnknownActionTypeError",
    "cancel_pending",
    "get_handler",
    "purge_for_contact",
    "register_handler",
    "registered_types",
    "schedule",
    "schedule_system",
]

logger = logging.getLogger(__name__)

#: ``handler(payload, action) -> None``. See the module docstring for the contract.
Handler = Callable[[dict[str, Any], ScheduledAction], None]

_HANDLERS: dict[str, Handler] = {}


class DuplicateHandlerError(RuntimeError):
    """Two handlers registered for one action type."""


class UnknownActionTypeError(LookupError):
    """A claimed row names a type nothing has registered a handler for."""


class IdempotencyKeyConflictError(RuntimeError):
    """An idempotency key is already in use by a different workspace."""


def register_handler(action_type: str, *, replace: bool = False) -> Callable[[Handler], Handler]:
    """Decorator mapping an action type to the callable that runs it.

    ``replace=True`` is for tests and for a deliberate override; without it a
    second registration for the same type raises.
    """

    def decorator(func: Handler) -> Handler:
        existing = _HANDLERS.get(action_type)
        if existing is not None and not replace and existing is not func:
            raise DuplicateHandlerError(
                f"{action_type!r} is already handled by "
                f"{existing.__module__}.{getattr(existing, '__qualname__', existing)}. "
                f"Action types are a shared namespace across every app; pick a distinct name, "
                f"or pass replace=True if the override is deliberate."
            )
        _HANDLERS[action_type] = func
        logger.debug("Registered queue handler %s -> %s.%s", action_type, func.__module__, func.__qualname__)
        return func

    return decorator


def get_handler(action_type: str) -> Handler | None:
    """The registered handler, or ``None`` when nothing claims this type."""
    return _HANDLERS.get(action_type)


def registered_types() -> tuple[str, ...]:
    """Every type with a handler, sorted. Ops visibility and error messages."""
    return tuple(sorted(_HANDLERS))


def schedule(
    action_type: str,
    run_at: datetime,
    payload: dict[str, Any] | None = None,
    *,
    workspace: Any,
    contact: Any = None,
    idempotency_key: str | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> ScheduledAction:
    """Enqueue one action for a workspace. Returns the row — new, or the one already there.

    ``workspace`` is required, keyword-only, and **must not be None**. A NULL
    workspace means a deployment-level system row, which no ``.for_workspace()``
    query can ever see (``apps.queueing.models``) — so a caller that passed
    ``None`` by accident would mint a tenant's work as an invisible system row
    and never hear about it. ``request.workspace`` is ``None`` on every
    anonymous and non-``/w/`` request, which is exactly how that accident
    happens. Use :func:`schedule_system` when a system row is what you actually
    mean; it is a separate call for the same reason ``.unscoped()`` is
    (CONTRIBUTING.md) — crossing the tenant boundary should be greppable, not a
    falsy argument.

    ``contact`` accepts a ``Contact``, a UUID or a string, so callers need not
    reach for ``.pk``.

    ``idempotency_key`` makes the enqueue a no-op when the key is already
    present: the existing row is returned unchanged, whatever its status.
    That is what an idempotency key means — "this work has already been
    arranged" — and it is why a key that has already run to ``done`` does not
    schedule a second run.
    """
    if workspace is None:
        raise ValueError(
            "schedule() needs a workspace. None would create a deployment-level system row that "
            "no tenant query can see, which is silent data loss from the owning workspace's point "
            "of view. Use schedule_system() if a system row is genuinely what you want."
        )
    return _schedule(
        action_type,
        run_at,
        payload,
        workspace=workspace,
        contact=contact,
        idempotency_key=idempotency_key,
        max_attempts=max_attempts,
    )


def schedule_system(
    action_type: str,
    run_at: datetime,
    payload: dict[str, Any] | None = None,
    *,
    idempotency_key: str | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> ScheduledAction:
    """Enqueue one action that belongs to the deployment rather than a tenant.

    The housekeeping chain is the only caller today. The row carries a NULL
    workspace, so it is invisible to every ``.for_workspace()`` query and
    reachable only from the worker's cross-tenant drain — which is the point,
    and which is why this is a separate, greppable entry point rather than
    ``schedule(workspace=None)``.

    Never call this with data belonging to a workspace.
    """
    return _schedule(
        action_type,
        run_at,
        payload,
        workspace=None,
        contact=None,
        idempotency_key=idempotency_key,
        max_attempts=max_attempts,
    )


def cancel_pending(workspace: Any, **filters: Any) -> int:
    """Cancel every ``pending`` row matching ``filters``. Returns how many.

    The disarm-side counterpart to :func:`schedule`: this app owns the
    mechanics (which statuses exist, and that ``pending`` is the only one safe
    to cancel), and a caller owns the predicate that says which rows are
    theirs to disarm — a contact's, a message's, an execution's.  ``filters``
    is passed straight to ``.filter()``, so it accepts whatever a caller's own
    query already used: ``contact_id=``, ``type=``/``type__in=``, a JSONB
    lookup like ``payload__message_id=``.

    Never touches a ``running`` row, deliberately. Cancelling one out from
    under a worker that already claimed it would not recall the handler
    inside it — every handler in this codebase re-checks the state it cares
    about before doing anything consequential, and a ``pending`` row is what
    actually stops a worker from ever picking the work up in the first place.
    """
    return (
        ScheduledAction.objects.for_workspace(workspace)
        .filter(status=ActionStatus.PENDING, **filters)
        .update(status=ActionStatus.CANCELLED, updated_at=timezone.now())
    )


def purge_for_contact(workspace: Any, contact_id: Any, *, exclude_action_id: Any = None) -> int:
    """Delete every row naming ``contact_id``. Returns how many went.

    The erasure-side counterpart to :func:`cancel_pending`, and a *delete* where
    that one is an update, because the two answer different questions.
    Cancelling disarms the work; issue #29's GDPR erasure has to remove the row
    itself. ``ScheduledAction.contact_id`` is a plain ``UUIDField`` and not a
    foreign key (see the column's comment), so nothing cascades it — and both
    ``payload`` and ``last_error`` can quote a message body, a rendered
    template or an address. A cancelled row is a row that still holds the text.

    **``running`` rows are left alone**, and the count excludes them. A worker
    that already claimed a row finishes by writing to it, and deleting it
    underneath would turn its ``update_fields`` save into "did not affect any
    rows" — a loud failure in an unrelated handler, for a row that is about to
    be irrelevant anyway. In practice the caller holds
    :func:`~apps.queueing.locks.contact_lock`, which is the same lock
    ``process_action`` takes before dispatching, so a live ``running`` row for
    this contact should not exist; the erasure records the count rather than
    assuming it is zero.

    ``exclude_action_id`` spares one row by primary key, and it is load-bearing
    on the queued path: the erasure's own action names the contact it is
    erasing, and a routine that deleted it would remove the row the worker is
    holding open.
    """
    rows = (
        ScheduledAction.objects.for_workspace(workspace)
        .filter(
            contact_id=coerce_contact_id(contact_id),
        )
        .exclude(status=ActionStatus.RUNNING)
    )
    if exclude_action_id is not None:
        rows = rows.exclude(pk=exclude_action_id)
    removed, _ = rows.delete()
    return int(removed)


def _schedule(
    action_type: str,
    run_at: datetime,
    payload: dict[str, Any] | None,
    *,
    workspace: Any,
    contact: Any,
    idempotency_key: str | None,
    max_attempts: int,
) -> ScheduledAction:
    """The shared body. Callers go through ``schedule`` or ``schedule_system``."""
    if get_handler(action_type) is None:
        # Not an error: an app may enqueue before the app that owns the type has
        # been imported, and the SPEC's own housekeeping chain enqueues its
        # successor from inside a handler. The worker fails the row loudly if
        # the type is still unclaimed by the time it is due.
        logger.warning(
            "Scheduling %r, which has no registered handler yet (known: %s)",
            action_type,
            ", ".join(registered_types()) or "none",
        )

    fields = {
        "workspace": workspace,
        "contact_id": coerce_contact_id(contact),
        "run_at": run_at,
        "type": action_type,
        "payload": payload or {},
        "status": ActionStatus.PENDING,
        "max_attempts": max_attempts,
        "idempotency_key": idempotency_key,
    }

    if idempotency_key is None:
        return ScheduledAction.objects.create(**fields)

    try:
        # A savepoint, so the IntegrityError below does not poison a transaction
        # the caller opened around this call.
        with transaction.atomic():
            return ScheduledAction.objects.create(**fields)
    except IntegrityError as exc:
        if not _is_idempotency_conflict(exc):
            # Some other constraint — a deleted workspace, a NOT NULL a later
            # migration added. Swallowing it here would surface as the
            # idempotency error below, sending the reader after a key collision
            # that never happened.
            raise

    existing = _existing_for_key(idempotency_key, workspace)
    logger.debug("Idempotent enqueue of %r hit existing action %s", action_type, existing.pk)
    return existing


def _is_idempotency_conflict(exc: IntegrityError) -> bool:
    """Was this integrity error the idempotency key, or something else entirely?

    psycopg exposes the violated constraint on the wrapped exception's ``diag``;
    Django's unique index for the field is named ``..._idempotency_key_..._uniq``.
    The message fallback covers a driver that reports no ``diag``.
    """
    constraint = getattr(getattr(exc.__cause__, "diag", None), "constraint_name", None)
    if constraint:
        return "idempotency_key" in constraint
    return "idempotency_key" in str(exc)


def _existing_for_key(idempotency_key: str, workspace: Any) -> ScheduledAction:
    """Re-read the row that won the unique index, scoped to the caller.

    Scoped deliberately. The key column is globally unique, so an unscoped read
    would hand one workspace a row belonging to another — a tenancy leak dressed
    up as a convenience. Keys are built from ids the caller already owns
    (``exec:{execution_id}:node:{node_id}``, SPEC §9.4), so a cross-tenant
    collision means a caller invented a guessable key, and that deserves an
    exception rather than a shrug.
    """
    if workspace is None:
        # System rows: no tenant owns them, so .unscoped() is the correct read
        # and the only one that can see a NULL-workspace row at all.
        existing = ScheduledAction.objects.unscoped().filter(idempotency_key=idempotency_key).first()
        if existing is not None and existing.workspace_id is None:
            return existing
    else:
        existing = ScheduledAction.objects.for_workspace(workspace).filter(idempotency_key=idempotency_key).first()
        if existing is not None:
            return existing

    raise IdempotencyKeyConflictError(
        f"Idempotency key {idempotency_key!r} is already held by a row this caller does not own. "
        f"Keys are globally unique; build them from ids belonging to the scheduling workspace."
    )
