"""The hourly sweep that stops a broadcast getting stuck (SPEC §15).

Registered through ``apps.queueing.housekeeping.register_housekeeping_job`` from
this app's ``ready()``, which is the mechanism that module's docstring describes:
a job takes no arguments, runs across every tenant, and must be idempotent
because the sweep retries as a whole if any job in it fails.

There is exactly one hole this closes, and it is worth naming because it is not
hypothetical. ``handle_broadcast_send`` records an outcome for every recipient it
reaches — but a handler that *raises* rolls its transaction back, and after
``max_attempts`` the queue marks the action ``failed`` and stops. The recipient
row is then ``pending`` with nothing left in the queue to move it, so the
broadcast never reaches "no pending recipients" and never settles: it sits at
``sending`` forever, its counters never reconcile, and ``broadcast.finished``
never fires for a broadcast that is, to any observer, finished.

So this reconciles a recipient against the action that was supposed to send it,
and then settles anything that has come to rest. Both halves are set-wise and
bounded to broadcasts that are actually live.

Registered without ``replace=True``, for the reason ``apps/broadcasts/handlers.py``
gives about the handler registry: a second claim on this name is a bug worth
hearing about at startup rather than one that resolves by import order.
"""

import logging

from django.db.models import Q
from django.utils import timezone

from apps.broadcasts.models import Broadcast, BroadcastRecipient, BroadcastStatus, RecipientStatus
from apps.broadcasts.services import settle
from apps.messaging.codes import Failure
from apps.queueing.housekeeping import register_housekeeping_job
from apps.queueing.models import ActionStatus, ScheduledAction

logger = logging.getLogger(__name__)

__all__ = ["settle_broadcasts"]


@register_housekeeping_job("settle_broadcasts")
def settle_broadcasts() -> str | None:
    """Reconcile abandoned recipients, then finish the broadcasts that are done."""
    # Cross-tenant on purpose: housekeeping drains the whole deployment, which is
    # what housekeeping *is* (CONTRIBUTING.md's rule for .unscoped()).
    live = list(Broadcast.objects.unscoped().filter(status=BroadcastStatus.SENDING))
    if not live:
        return None

    stranded = 0
    finished = 0
    for broadcast in live:
        stranded += _reconcile(broadcast)
        if settle(broadcast):
            finished += 1

    if not stranded and not finished:
        return None
    return f"reconciled {stranded} stranded recipient(s), finished {finished} broadcast(s)"


def _reconcile(broadcast: Broadcast) -> int:
    """Fail the recipients whose send action gave up, so the broadcast can settle.

    The join is done by the action's idempotency key, which SPEC §13.2 fixes as
    ``broadcast:<id>:contact:<id>`` — a value this app mints, so it is a reliable
    way back from an action to a recipient without the queue growing a foreign
    key to a Layer-6 app.
    """
    pending = BroadcastRecipient.objects.for_workspace(broadcast.workspace_id).filter(
        broadcast=broadcast, status=RecipientStatus.PENDING
    )
    contact_ids = list(pending.values_list("contact_id", flat=True))
    if not contact_ids:
        return 0

    keys = {f"broadcast:{broadcast.pk}:contact:{contact_id}": contact_id for contact_id in contact_ids}
    alive = set(
        ScheduledAction.objects.for_workspace(broadcast.workspace_id)
        .filter(idempotency_key__in=list(keys))
        .filter(Q(status=ActionStatus.PENDING) | Q(status=ActionStatus.RUNNING))
        .values_list("idempotency_key", flat=True)
    )
    abandoned = [contact_id for key, contact_id in keys.items() if key not in alive]
    if not abandoned:
        return 0

    updated = pending.filter(contact_id__in=abandoned).update(
        status=RecipientStatus.FAILED, reason=Failure.RETRIES_EXHAUSTED.value, updated_at=timezone.now()
    )
    if updated:
        logger.warning("Broadcast %s: %s recipient(s) had no live send action left", broadcast.pk, updated)
    return int(updated)
