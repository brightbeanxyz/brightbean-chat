"""The only place this app talks to ``apps.queueing`` (issue #5).

#5 is a **parallel Layer-2 sibling**: at the time this was written it had not
merged, and ``apps.queueing`` does not exist in the tree. ROADMAP's rule for
that situation is to code against the written contract rather than a sibling
branch, so this module is deliberately the entire surface — nothing else in
``apps.notifications`` imports ``apps.queueing``, and if #5 lands with a
different spelling, this one file is the repair.

The contract as written (issue #5, ``docs/SPEC.md`` §15):

* a handler registry mapping a ``scheduled_action.type`` string to a callable,
  registered additively by the owning app — **never by editing #5's module**;
* an enqueue entry point taking that type plus a JSON payload.

Two things are load-bearing about the shape below.

**Binding is probed, not assumed.** A single hard-coded import path that #5
spells differently would fail silently into the synchronous fallback: email
would still be sent, so no test would go red, and the queue would simply never
be used. Probing a short ordered list of plausible module paths and *logging
which one bound* makes the state observable instead.

**A signature mismatch degrades rather than drops.** If ``enqueue`` exists but
rejects our keywords, the caller sends inline. Losing the email would be worse
than losing the queueing.
"""

import importlib
import logging
from collections.abc import Callable
from typing import Any

from django.apps import apps as django_apps

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

_REGISTER_LOCATIONS = ("apps.queueing.handlers", "apps.queueing.registry", "apps.queueing")
_ENQUEUE_LOCATIONS = ("apps.queueing.services", "apps.queueing.scheduling", "apps.queueing")


#: Resolved entry points, keyed by the attribute name. The probe walks a short
#: module list and logs what it bound; doing that per notification meant a
#: 500-recipient fan-out repeated the lookup 500 times and wrote 500 identical
#: INFO lines. ``reset_bindings()`` exists for tests, which install and remove
#: a stand-in ``apps.queueing`` between cases.
_BINDINGS: dict[str, Callable[..., Any] | None] = {}


def reset_bindings() -> None:
    """Forget what was bound. For tests that swap ``apps.queueing`` in and out."""
    _BINDINGS.clear()


def _bind(module_names: tuple[str, ...], attr: str) -> Callable[..., Any] | None:
    if attr in _BINDINGS:
        return _BINDINGS[attr]
    _BINDINGS[attr] = _probe(module_names, attr)
    return _BINDINGS[attr]


def _probe(module_names: tuple[str, ...], attr: str) -> Callable[..., Any] | None:
    # is_installed() first: it is cheaper than an import and it is the correct
    # question. A package that is importable but absent from INSTALLED_APPS has
    # no migrated tables, so enqueueing into it would fail at the database.
    if not django_apps.is_installed("apps.queueing"):
        return None
    for module_name in module_names:
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        candidate = getattr(module, attr, None)
        if callable(candidate):
            logger.info("apps.notifications bound %s to %s.%s", attr, module_name, attr)
            return candidate  # type: ignore[no-any-return]
    return None


def queueing_available() -> bool:
    """Whether an enqueue entry point can be reached right now."""
    return _bind(_ENQUEUE_LOCATIONS, "enqueue") is not None


class NotificationEmailError(RuntimeError):
    """A queued send failed and the action should be retried.

    The queue decides *whether* to retry and how long to wait (SPEC §15:
    30s, 2m, 10m, 1h, 6h, then failed). Its only signal is whether the handler
    raised, so a handler that swallows a transport error tells it the send
    succeeded — and a transient SMTP outage becomes permanent.
    """


def handle_notification_email(payload: dict[str, Any]) -> None:
    """The worker-side half: send the email a scheduled action stands for.

    Kept tiny and import-light on purpose — it is called from #5's worker
    process, which has no reason to import this app's views or engine.

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
    """Register :data:`HANDLER_TYPE` with #5's registry. No-op when it is absent."""
    register = _bind(_REGISTER_LOCATIONS, "register_handler")
    if register is None:
        logger.info("apps.queueing is not installed; notification email will send synchronously.")
        return False
    try:
        register(HANDLER_TYPE, handle_notification_email)
    except (TypeError, ValueError):
        logger.exception("Could not register the %r handler with apps.queueing.", HANDLER_TYPE)
        return False
    return True


def enqueue_email(delivery: Any, *, workspace_id: Any = None) -> bool:
    """Hand this delivery to the queue. ``False`` means "send it inline instead".

    ``idempotency_key`` is the delivery id: SPEC §15 puts a unique index on that
    column, so a double-enqueue of the same delivery is refused by the database
    rather than sending the same email twice.
    """
    enqueue = _bind(_ENQUEUE_LOCATIONS, "enqueue")
    if enqueue is None:
        return False
    try:
        enqueue(
            type=HANDLER_TYPE,
            payload={"delivery_id": str(delivery.pk)},
            workspace_id=workspace_id,
            idempotency_key=f"{HANDLER_TYPE}:{delivery.pk}",
        )
    except TypeError:
        logger.exception(
            "apps.queueing.enqueue rejected the notification_email call signature; "
            "sending inline. Repair apps/notifications/queue.py — it is the only "
            "module that talks to apps.queueing."
        )
        return False
    return True
