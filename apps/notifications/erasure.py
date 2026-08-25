"""Notifications that name a contact, and why they need removing by hand.

Three registered events put a person's display name into the copy an operator
reads: the loop-cap and execution-failed events (``apps/notifications/events.py``
— "It ran 30 blocks without pausing for {contact_name}", "The run for
{contact_name} stopped at…") and the inbox reminder and scheduled-reply-failed
events (``apps/inbox/notifications.py``). :func:`apps.notifications.engine.notify`
renders that copy at write time, so the name is stored in ``Notification.title``
and ``Notification.body`` rather than being formatted on the way out.

That makes these rows a genuine residue of issue #29's erasure, and an awkward
one: :class:`~apps.notifications.models.Notification` is a ``BaseModel``
addressed to a *user*, with no workspace column and no contact column — by
design, because the bell reads across every workspace a person belongs to. So
nothing cascades, and there was nothing to match on either.

The fix is at the three call sites rather than here: each now passes a
``contact_id`` into the notification context, which ``engine._payload`` copies
into the ``payload`` jsonb because it is a json-safe value. That gives this
module a queryable handle and costs the rendered copy nothing — ``contact_id``
appears in no template.

**Deleting the whole row is the only correct answer.** Blanking ``title`` and
``body`` would leave a bell entry that says nothing, and the operator's alert
about a run that failed is worthless once the contact it concerned is gone.

Reached from ``apps.contacts`` through ``apps.contacts.activity``, guarded by
``installed_model``. Nothing here imports contacts.
"""

import logging
from typing import Any

from apps.notifications.models import Notification

logger = logging.getLogger(__name__)

__all__ = ["erase_for_contact"]


def erase_for_contact(workspace_id: Any, contact_id: Any) -> dict[str, int]:
    """Delete notifications naming ``contact_id``. ``{label: rows}``.

    Filtered on ``workspace_id`` as well as the contact, even though a UUIDv7
    collision across tenants is not a thing that happens: the id arrives from a
    caller, this model has no tenancy guard of its own to fall back on, and a
    query that would do the wrong thing given the wrong argument is worth
    narrowing whether or not the argument can be wrong today.

    ``NotificationDelivery`` rows cascade from the notification and carry no
    copy of their own.
    """
    rows = Notification.objects.filter(
        payload__workspace_id=str(workspace_id),
        payload__contact_id=str(contact_id),
    )
    removed = int(rows.count())
    if not removed:
        return {}
    rows.delete()
    logger.info("Erasure: removed %s notification(s) naming contact %s.", removed, contact_id)
    return {"notifications.Notification": removed}
