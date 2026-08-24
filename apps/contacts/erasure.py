"""GDPR erasure: removing a contact for real (SPEC §19, issue #29).

``delete_contact`` sets ``status=deleted`` and stops there, deliberately —
:mod:`apps.contacts.services` says so, and says this module owns the rest. A
tombstone hides someone from every read surface; it does not answer a right-to-
erasure request, because their identities, their consent records and their
message bodies are all still in the database.

--------------------------------------------------------------------------
The foreign-key graph does most of the work, and that is on purpose
--------------------------------------------------------------------------

``Contact.delete()`` is one statement, and it cascades identities,
conversations, every message in them, every conversation-scoped row in
``apps.inbox`` (notes, labels, reminders, and a scheduled reply's drafted
``body``), flow executions with their collected ``variables``, default-reply
state, sequence enrollments and rule-trigger fires. Hand-writing any of that
here would be a second description of a rule the database already enforces, and
the two would drift the first time somebody added a model.

So this module contributes only what a cascade *cannot* reach, and each piece
lives in the app that owns those rows — the direction ``merge_contacts`` already
established, through :mod:`apps.contacts.activity` and ``installed_model``:

* ``queueing.ScheduledAction`` — ``contact_id`` is a plain ``UUIDField``, not a
  foreign key, so nothing cascades it, and ``payload`` and ``last_error`` can
  quote a rendered message.
* ``flows.HandledComment`` — ``SET_NULL``, and the row keeps ``commenter_ref``.
* ``notifications.Notification`` — no workspace column and no contact column,
  with a display name baked into the stored copy.
* ``broadcasts.BroadcastRecipient`` — ``SET_NULL`` *by design*, because it is an
  anonymised counter that has to survive; it needs its verdict written down
  before the evidence goes.

Whether that list is complete is not a claim this docstring makes. It is
asserted by ``apps/contacts/tests/test_erasure.py``, which walks the model graph
for every reference to ``Contact`` and fails on one nobody has classified.

--------------------------------------------------------------------------
Order, and the lock
--------------------------------------------------------------------------

Stand the contact down *before* tombstoning it, and tombstone it before the
teardown: a live execution must be expired while the engine will still accept
the contact, and every read surface should 404 the moment the erasure is
accepted rather than when it finishes. The destructive half then runs under
``contact_lock`` — the same advisory lock ``process_action`` takes before
dispatching a handler (SPEC §9.6) — which is what makes "nothing is written
while we delete" true rather than hoped, and what makes it safe to delete queue
rows out from under a worker that would otherwise be running one.

--------------------------------------------------------------------------
Two paths
--------------------------------------------------------------------------

A contact with a handful of messages is erased in the request. A contact with
tens of thousands is not: that is a long transaction holding a lock, and SPEC
§15 has a worker for exactly this. Either way a :class:`ContactErasure` row is
committed *first*, so a crash leaves a record that the request was made — the
same reason ``ContactImport`` exists before its work does.
"""

import logging
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.contacts import activity
from apps.contacts import services as contact_services
from apps.contacts.errors import ContactsError
from apps.contacts.models import Contact, ContactErasure, ErasureStatus
from apps.queueing.locks import contact_lock
from apps.queueing.registry import purge_for_contact, register_handler, schedule

logger = logging.getLogger(__name__)

__all__ = [
    "ACTION_TYPE",
    "CONFIRMATION",
    "ErasureRefusedError",
    "begin",
    "handle_contact_erasure",
    "run",
    "should_queue",
]

#: The queue action type. ``ActionType`` is an open set and the registry is the
#: authority, so this needs no migration in ``apps.queueing``.
ACTION_TYPE = "contact_erasure"

#: What an operator types to confirm. A fixed word, **not** the contact's name:
#: ``display_name`` can be empty, can be a ``Contact 0193a…`` fallback, and
#: asking someone to type a real person's name puts it in a request body for the
#: sake of a speed bump.
CONFIRMATION = "ERASE"


class ErasureRefusedError(ContactsError):
    """The request was not accepted. Carries a message written for an operator."""


def should_queue(contact: Contact) -> bool:
    """Whether this contact is too big to erase inside a web request."""
    return activity.message_count(contact) > settings.CONTACT_ERASURE_SYNC_MAX_MESSAGES


def begin(
    contact: Contact,
    *,
    source: str,
    requested_by: Any = None,
    api_key_id: Any = None,
    force_queue: bool = False,
) -> ContactErasure:
    """Accept an erasure request. The single entry point for every surface.

    Returns the audit row. Its ``status`` says which path was taken: ``done``
    when the work happened inline, ``pending`` when it was handed to the queue.

    Refuses a second live request for the same contact — the partial unique
    constraint would do it anyway, but an ``IntegrityError`` is a 500 and a
    double-clicked button deserves a sentence.
    """
    live = ContactErasure.objects.for_workspace(contact.workspace_id).filter(
        contact_id=contact.pk,
        status__in=(ErasureStatus.PENDING, ErasureStatus.RUNNING),
    )
    if live.exists():
        raise ErasureRefusedError("An erasure for this contact is already running.")

    queued = force_queue or should_queue(contact)

    with transaction.atomic():
        record = ContactErasure.objects.create(
            workspace=contact.workspace,
            contact_id=contact.pk,
            source=source,
            requested_by=requested_by,
            # Denormalised deliberately: the foreign key answers "nobody" once
            # the account goes, which is when an audit trail is most often read.
            requested_by_label=str(getattr(requested_by, "email", "") or "")[:254],
            api_key_id=api_key_id,
            status=ErasureStatus.PENDING,
        )

        # Both before the tombstone, and in this order. ``stand_down`` expires
        # live executions and cancels the queue rows that would resume them,
        # which the engine will only do for a contact it still considers real;
        # the tombstone then takes the contact off every read surface, so
        # "delete → export 404s" holds from the moment the request is accepted
        # rather than from the moment a worker gets to it.
        activity.stand_down(contact)
        contact_services.delete_contact(contact)

        if queued:
            _enqueue(record, contact)
            return record

    return run(record, contact=contact)


def run(record: ContactErasure, *, contact: Contact | None = None, action: Any = None) -> ContactErasure:
    """Do the destructive half. Idempotent: a finished record is returned as is.

    ``action`` is the queue row running this, when there is one. It is excluded
    from the queue purge for the obvious reason — it names the contact being
    erased, and deleting it would remove the row the worker is holding open.
    """
    if record.status == ErasureStatus.DONE:
        return record

    if contact is None:
        contact = _contact_for(record)
    if contact is None:
        # The row is already gone: a retry after the delete committed but before
        # the record was stamped. The requested state holds, so complete rather
        # than raising — five attempts over six hours cannot un-delete it.
        return _finish(record, counts={})

    counts: dict[str, int] = {}
    with transaction.atomic(), contact_lock(contact):
        ContactErasure.objects.for_workspace(record.workspace_id).filter(pk=record.pk).update(
            status=ErasureStatus.RUNNING, updated_at=timezone.now()
        )

        counts.update(activity.tear_down(contact))
        purged = purge_for_contact(
            record.workspace_id,
            contact.pk,
            exclude_action_id=getattr(action, "pk", None),
        )
        if purged:
            counts["queueing.ScheduledAction"] = purged

        # One statement, and everything the graph knows about goes with it.
        # Django's collector reports what it took, which *is* the audit receipt
        # — a count per model label, derived rather than hand-maintained.
        _total, cascaded = contact.delete()
        for label, rows in cascaded.items():
            counts[label] = counts.get(label, 0) + int(rows)

        record.refresh_from_db()
        return _finish(record, counts=counts)


def _finish(record: ContactErasure, *, counts: dict[str, int]) -> ContactErasure:
    record.status = ErasureStatus.DONE
    record.completed_at = timezone.now()
    record.counts = counts
    record.error = ""
    record.save(update_fields=["status", "completed_at", "counts", "error", "updated_at"])
    logger.info("Erased contact %s: %s", record.contact_id, counts)
    return record


def _contact_for(record: ContactErasure) -> Contact | None:
    """The contact this record names, tombstone included.

    ``all_objects`` would skip the workspace scoping this table is entitled to,
    so the scoped manager is used and the status is simply not filtered — the
    row is a tombstone by the time any of this runs, and a lookup that insisted
    on ``ACTIVE`` could never find its own subject.
    """
    return Contact.objects.for_workspace(record.workspace_id).filter(pk=record.contact_id).first()


def _enqueue(record: ContactErasure, contact: Contact) -> Any:
    """Hand the work to a worker, naming the contact.

    Naming it is what makes ``process_action`` take ``contact_lock`` before
    dispatch, so the handler inherits the serialisation this needs instead of
    arranging it again.

    No idempotency key, for the reason ``imports.enqueue`` gives about its own:
    the obvious key would make a legitimate retry a silent no-op, and what
    actually prevents two concurrent teardowns is the partial unique constraint
    on the record plus the contact lock.
    """
    return schedule(
        ACTION_TYPE,
        timezone.now(),
        {"erasure_id": str(record.pk)},
        workspace=record.workspace,
        contact=contact,
    )


@register_handler(ACTION_TYPE)
def handle_contact_erasure(payload: dict[str, Any], action: Any) -> None:
    """Run a queued erasure.

    A record that has vanished is not an error worth retrying — nothing can
    reconstruct it — so it is logged and dropped rather than raised, the same
    division ``handle_contact_import`` draws between a row failure and a run
    failure. Anything else propagates onto SPEC §15's backoff ladder.
    """
    erasure_id = str(payload.get("erasure_id") or "")
    record = (
        ContactErasure.objects.for_workspace(action.workspace_id).filter(pk=erasure_id).first() if erasure_id else None
    )
    if record is None:
        logger.warning("Erasure %s is gone; nothing to run.", payload.get("erasure_id"))
        return
    run(record, action=action)
