"""What a contact erasure has to remove from this app by hand (issue #29).

Almost nothing, and that is the point of writing it down. ``FlowExecution`` and
``DefaultReplyState`` both hold a ``CASCADE`` foreign key to ``Contact``, so
deleting the contact takes them — variables, wait config, collected answers and
all — without this module existing. Re-spelling a cascade in Python would be a
second description of the same rule, and the two would eventually disagree.

:class:`~apps.flows.models.HandledComment` is the exception, and the reason is
one word in its declaration: its ``contact`` foreign key is ``SET_NULL``, not
``CASCADE``. That is right for its own job — the row is a once-only guard keyed
on ``(connection, post_id, commenter_ref)``, and it has to keep working after a
merge re-points the contact — but it means a cascade leaves the row behind with
``commenter_ref`` intact, which is the commenter's platform user id. A platform
user id is exactly the identifier the rest of the erasure is removing; leaving
it here would mean the erasure had missed the one row whose FK was written to
survive one.

Reached from ``apps.contacts`` through ``apps.contacts.activity``, which is that
app's single door to this one, guarded by ``installed_model``. Nothing here
imports contacts.
"""

import logging
from typing import Any

from apps.flows.models import HandledComment

logger = logging.getLogger(__name__)

__all__ = ["erase_for_contact"]


def erase_for_contact(contact: Any) -> dict[str, int]:
    """Remove this app's non-cascading rows for ``contact``. ``{label: rows}``.

    Must run **before** the contact row goes: ``SET_NULL`` means the link is
    what identifies these rows, and once it is null there is nothing left to
    match on. The orchestrator's ordering is not incidental.

    Returns an empty mapping rather than a zero when there was nothing to do,
    so the audit record's counts carry only what actually happened.
    """
    removed, _ = HandledComment.objects.for_workspace(contact.workspace_id).filter(contact=contact).delete()
    if not removed:
        return {}
    logger.info("Erasure: removed %s handled comment(s) for contact %s.", removed, contact.pk)
    return {"flows.HandledComment": int(removed)}
