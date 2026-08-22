"""The only place this app talks to ``apps.queueing`` (issue #5).

Nothing else in ``apps.notifications`` imports ``apps.queueing``, so this one
file is the repair if that API ever moves.

**This module was written against a guess and every part of the guess was
wrong.** #5 was an unmerged parallel sibling at the time, so the seam probed a
short list of plausible module paths for ``register_handler`` and ``enqueue``,
and degraded to a synchronous send whenever a name was missing or a signature
was rejected. #5 then shipped ``apps.queueing.registry.schedule`` — not
``enqueue`` — with ``register_handler`` as a *decorator factory* and handlers
taking ``(payload, action)``. All three probes therefore missed, every miss was
caught and logged, and email quietly kept sending inline while the test suite
stayed green, because the fake in ``tests/test_queue.py`` was built from the
same guess.

So the degradation is now much narrower, on purpose:

* **The module path is not guessed.** ``apps.queueing.registry`` is imported by
  name. A missing attribute is a programming error and raises.
* **A signature mismatch raises.** It is a bug in *this* file, and the previous
  behaviour — catch ``TypeError``, log, send inline — is exactly what hid this
  one for a whole layer. Losing an email is bad; silently losing the queue for
  every email is worse, and it is invisible.
* **Only "the app is not installed" still degrades**, because that is a real
  deployment shape rather than a mistake, and the fallback genuinely works.

Reaching the real API is asserted by ``tests/test_queue.py``'s contract test,
which calls it rather than a stand-in.
"""

import importlib
import logging
from collections.abc import Callable
from typing import Any

from django.apps import apps as django_apps
from django.utils import timezone

logger = logging.getLogger(__name__)

__all__ = [
    "HANDLER_TYPE",
    "NotificationEmailError",
    "reset_bindings",
    "enqueue_email",
    "handle_notification_email",
    "queueing_available",
    "register_handler_if_available",
]

#: The ``scheduled_action.type`` this app owns. Additive to the enum SPEC §15
#: writes out (resume_execution, start_flow, sequence_step, broadcast_fanout,
#: broadcast_send, send_retry, followup_timer, housekeeping).
HANDLER_TYPE = "notification_email"

#: #5's public surface. One module, named rather than searched for.
_QUEUEING_REGISTRY = "apps.queueing.registry"


#: Resolved entry points, keyed by the attribute name. The probe walks a short
#: module list and logs what it bound; doing that per notification meant a
#: 500-recipient fan-out repeated the lookup 500 times and wrote 500 identical
#: INFO lines. ``reset_bindings()`` exists for tests, which install and remove
#: a stand-in ``apps.queueing`` between cases.
_BINDINGS: dict[str, Callable[..., Any] | None] = {}


def reset_bindings() -> None:
    """Forget what was bound. For tests that swap ``apps.queueing`` in and out."""
    _BINDINGS.clear()


def _bind(attr: str) -> Callable[..., Any] | None:
    if attr in _BINDINGS:
        return _BINDINGS[attr]
    _BINDINGS[attr] = _resolve(attr)
    return _BINDINGS[attr]


def _resolve(attr: str) -> Callable[..., Any] | None:
    """The named entry point, or ``None`` when the queue app is not installed.

    ``is_installed()`` first: it is cheaper than an import and it is the correct
    question. A package that is importable but absent from ``INSTALLED_APPS``
    has no migrated tables, so enqueueing into it would fail at the database.

    A *missing attribute* on an installed app is a different thing entirely —
    this file naming something #5 does not export — and raises rather than
    degrading, because degrading is what hid the last one.
    """
    if not django_apps.is_installed("apps.queueing"):
        return None
    module = importlib.import_module(_QUEUEING_REGISTRY)
    entry_point = getattr(module, attr, None)
    if not callable(entry_point):
        raise AttributeError(
            f"{_QUEUEING_REGISTRY}.{attr} is missing or not callable. apps/notifications/queue.py "
            f"is the only module that talks to apps.queueing; repair it there."
        )
    logger.debug("apps.notifications bound %s to %s.%s", attr, _QUEUEING_REGISTRY, attr)
    return entry_point  # type: ignore[no-any-return]


def queueing_available() -> bool:
    """Whether the queue can be reached right now."""
    return _bind("schedule") is not None


class NotificationEmailError(RuntimeError):
    """A queued send failed and the action should be retried.

    The queue decides *whether* to retry and how long to wait (SPEC §15:
    30s, 2m, 10m, 1h, 6h, then failed). Its only signal is whether the handler
    raised, so a handler that swallows a transport error tells it the send
    succeeded — and a transient SMTP outage becomes permanent.
    """


def handle_notification_email(payload: dict[str, Any], action: Any = None) -> None:
    """The worker-side half: send the email a scheduled action stands for.

    Kept tiny and import-light on purpose — it is called from #5's worker
    process, which has no reason to import this app's views or engine.

    ``action`` is the ``ScheduledAction`` row: #5's ``Handler`` type is
    ``Callable[[dict, ScheduledAction], None]``, so the worker passes two
    arguments. It is unused here — the payload carries the delivery id, which is
    everything this handler needs — and defaulted so the function stays callable
    with one argument from a test or a direct call.

    Raises :class:`NotificationEmailError` when the send fails, so the queue's
    backoff applies. Returns quietly when there is nothing to do, so a retry
    that has been overtaken by events does not spin.
    """
    from apps.notifications.mail import send_delivery
    from apps.notifications.models import DeliveryStatus, NotificationDelivery

    delivery_id = payload.get("delivery_id")
    delivery = (
        NotificationDelivery.objects.filter(pk=delivery_id).select_related("notification__user").first()
        if delivery_id
        else None
    )
    if delivery is None:
        # The notification was deleted between enqueue and run. Nothing to do,
        # and nothing worth failing the action over.
        logger.info("notification_email action referenced a delivery that no longer exists: %r", delivery_id)
        return

    if delivery.status == DeliveryStatus.SENT:
        # The idempotency key stops a second *action row* being enqueued; it
        # does nothing about one row running twice. A worker that sent the mail
        # and died before marking the action done gets swept back to pending by
        # zombie recovery, and without this check the recipient is mailed again.
        logger.info("notification_email action re-ran for delivery %s, which was already sent.", delivery.pk)
        return

    if not send_delivery(delivery):
        # send_delivery has already recorded FAILED and logged the cause; this
        # exists purely to tell the queue the action did not succeed.
        raise NotificationEmailError(f"Notification email delivery {delivery.pk} failed; queued for retry.")


def register_handler_if_available() -> bool:
    """Register :data:`HANDLER_TYPE` with #5's registry. No-op when it is absent.

    ``register_handler`` is a **decorator factory** — ``register(type)(func)``,
    not ``register(type, func)``. Calling it the second way raised ``TypeError``,
    which the old body caught and logged, so registration failed on every boot
    and every notification email silently sent inline instead.

    ``replace=True`` because ``AppConfig.ready()`` runs twice under some
    autoreload paths, and #5 refuses a second registration of the same type
    otherwise.
    """
    register = _bind("register_handler")
    if register is None:
        logger.info("apps.queueing is not installed; notification email will send synchronously.")
        return False
    register(HANDLER_TYPE, replace=True)(handle_notification_email)
    return True


def enqueue_email(delivery: Any, *, workspace: Any = None) -> bool:
    """Hand this delivery to the queue. ``False`` means "send it inline instead".

    ``idempotency_key`` is the delivery id: SPEC §15 puts a unique index on that
    column, so a double-enqueue of the same delivery returns the row that won
    rather than sending the same email twice.

    Two entry points, because #5 draws a line this app has to respect.
    ``schedule()`` requires a workspace and ``schedule_system()`` writes a NULL
    one, and its docstring says "never call this with data belonging to a
    workspace" — a system row is invisible to every ``.for_workspace()`` query.
    A notification genuinely may have no workspace (an account-level one), so
    the choice is made here rather than by passing ``workspace=None`` into a
    function that does not accept it.

    ``run_at`` is now: SPEC §15's queue has no "immediate" mode, and a due row
    is what the worker drains on its next pass.
    """
    schedule = _bind("schedule")
    if schedule is None:
        return False

    payload = {"delivery_id": str(delivery.pk)}
    idempotency_key = f"{HANDLER_TYPE}:{delivery.pk}"
    now = timezone.now()

    scoped = _as_workspace(workspace)
    if scoped is None:
        schedule_system = _bind("schedule_system")
        if schedule_system is None:  # pragma: no cover - both bind or neither does
            return False
        schedule_system(HANDLER_TYPE, now, payload, idempotency_key=idempotency_key)
        return True

    schedule(HANDLER_TYPE, now, payload, workspace=scoped, idempotency_key=idempotency_key)
    return True


def _as_workspace(workspace: Any) -> Any:
    """A ``Workspace`` instance, or ``None``.

    #5 assigns straight to the foreign key, so an id will not do. Callers hold
    an instance in the normal case and this is free; the id path costs one query
    and exists because ``notify()`` accepts either.
    """
    if workspace is None:
        return None
    if hasattr(workspace, "_meta"):
        return workspace
    from apps.workspaces.models import Workspace

    return Workspace.objects.filter(pk=workspace).first()
