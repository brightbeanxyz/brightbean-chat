"""Keeping a broadcast's counters true when one of its recipients is erased.

Issue #29's GDPR erasure removes a contact outright. ``BroadcastRecipient`` is
the only counter row in the product that names one, and SPEC §19 asks in the
same sentence for the hard delete and for anonymized counters to be kept — so
the foreign key is ``SET_NULL`` (see the field's comment) and the row survives
carrying a status, a machine-readable ``reason`` and no personal data at all.

Nulling the column is not enough on its own, though, because
:func:`apps.broadcasts.services.counters` does not read the stored statuses
verbatim. It recomputes a finished broadcast's figures live, and two of its
rules reach through columns the erasure is about to empty:

* a ``pending`` recipient keeps the broadcast from settling, and an erased
  contact's copy is never going to be sent;
* a ``sent`` recipient whose *message* ended ``failed`` is reclassified as
  failed at read time — "the message row has the last word on a send that
  already went out". Delete the message and that reclassification silently
  stops, so the live recount drifts back **above** the frozen ``stats`` json
  that ``settle()`` wrote.

Both are fixed the same way: write the verdict onto the recipient row itself
before the evidence goes. Afterwards the live recompute and the frozen snapshot
agree, which is what "counters still reconcile" means.

Reached from ``apps.contacts`` through ``apps.contacts.activity``, guarded by
``installed_model``. Nothing here imports contacts.
"""

import logging
from typing import Any

from apps.broadcasts import services
from apps.broadcasts.handlers import _settle_recipient
from apps.broadcasts.models import Broadcast, BroadcastRecipient, BroadcastStatus, RecipientStatus
from apps.messaging.codes import Denial

logger = logging.getLogger(__name__)

__all__ = ["prepare_for_erasure"]

#: Message statuses ``services.counters`` treats as a failed send. Imported
#: rather than re-spelled: if that rule changes, this must change with it or the
#: reconciliation this module exists to protect quietly stops holding.
_FAILED_MESSAGE_STATUSES = services._FAILED_STATUSES


def prepare_for_erasure(contact: Any) -> dict[str, int]:
    """Settle ``contact``'s recipient rows so they still count once it is gone.

    Runs **before** the delete, inside the erasure's transaction and under its
    contact lock. Returns ``{label: rows}`` for the audit record.
    """
    rows = BroadcastRecipient.objects.for_workspace(contact.workspace_id).filter(contact=contact)

    reclassified = _freeze_failed_sends(rows)
    skipped, touched = _skip_pending(rows)

    for broadcast in touched:
        # A broadcast whose last outstanding recipient was this contact can now
        # come to rest. Without this it stays ``sending`` until the hourly
        # ``settle_broadcasts`` sweep notices, and its ``stats`` json — the
        # anonymized counter this whole module is protecting — is not written
        # until it settles.
        services.settle(broadcast)

    counts: dict[str, int] = {}
    if reclassified:
        counts["broadcasts.BroadcastRecipient.reclassified"] = reclassified
    if skipped:
        counts["broadcasts.BroadcastRecipient.skipped"] = skipped
    return counts


def _freeze_failed_sends(rows: Any) -> int:
    """Write the joined message's failure onto the recipient that reads it.

    Only ``sent`` rows, and only where the message actually failed: any other
    row already says on its own what ``counters`` would conclude.
    """
    stale = rows.filter(status=RecipientStatus.SENT, message__status__in=_FAILED_MESSAGE_STATUSES)
    frozen = 0
    for recipient in stale.select_related("message"):
        _settle_recipient(recipient, RecipientStatus.FAILED, recipient.message.error or "")
        frozen += 1
    if frozen:
        logger.info("Erasure: froze %s reclassified-failed recipient row(s).", frozen)
    return frozen


def _skip_pending(rows: Any) -> tuple[int, list[Broadcast]]:
    """Record the copies that will now never be sent, and say why.

    ``Denial.CONTACT_DELETED`` is the code the send handler already uses when it
    finds the contact gone at delivery time, so the reason on the row reads the
    same whether the erasure arrived before the fanout or during it.
    """
    pending = rows.filter(status=RecipientStatus.PENDING).select_related("broadcast")
    touched: dict[Any, Broadcast] = {}
    skipped = 0
    for recipient in pending:
        _settle_recipient(recipient, RecipientStatus.SKIPPED, Denial.CONTACT_DELETED.value)
        skipped += 1
        if recipient.broadcast.status == BroadcastStatus.SENDING:
            touched[recipient.broadcast_id] = recipient.broadcast
    if skipped:
        logger.info("Erasure: skipped %s pending recipient row(s).", skipped)
    return skipped, list(touched.values())
