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
from uuid import UUID

from django.db import IntegrityError, transaction

from apps.queueing.models import DEFAULT_MAX_ATTEMPTS, ActionStatus, ScheduledAction

__all__ = [
    "DuplicateHandlerError",
    "Handler",
    "IdempotencyKeyConflictError",
    "UnknownActionTypeError",
    "get_handler",
    "register_handler",
    "registered_types",
    "schedule",
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


def _contact_id(contact: Any) -> UUID | str | None:
    """Accept a Contact instance, a UUID or a string; return a UUID.

    Normalised rather than passed through, so ``action.contact_id`` is the same
    type whether the row was just created or just read back from the database.
    A caller that got a ``str`` from one path and a ``UUID`` from the other
    would compare the two and quietly get ``False``.
    """
    if contact is None:
        return None
    value = getattr(contact, "pk", contact)
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        # Not a UUID. Let the field raise on save rather than silently storing
        # something the column cannot hold.
        return str(value)


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
    """Enqueue one action. Returns the row — new, or the one already there.

    ``workspace`` is required and may be ``None`` **only** for deployment-level
    system work (the housekeeping chain); it is keyword-only so that passing it
    is a deliberate act rather than a positional accident. ``contact`` accepts a
    ``Contact``, a UUID or a string, so callers need not reach for ``.pk``.

    ``idempotency_key`` makes the enqueue a no-op when the key is already
    present: the existing row is returned unchanged, whatever its status.
    That is what an idempotency key means — "this work has already been
    arranged" — and it is why a key that has already run to ``done`` does not
    schedule a second run.
    """
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
        "contact_id": _contact_id(contact),
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
    except IntegrityError:
        pass

    existing = _existing_for_key(idempotency_key, workspace)
    logger.debug("Idempotent enqueue of %r hit existing action %s", action_type, existing.pk)
    return existing


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
