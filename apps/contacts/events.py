"""The contact half of the internal event catalog (ROADMAP contract 7).

Four Django signals with **fixed dotted names**. Two later workstreams consume
them and neither exists yet: issue #22 (L6-A) turns them into rule triggers, and
issue #25 (L5-F) fans them out as outbound webhooks, storing the dotted string in
``outbound_webhook.events`` (SPEC §5). Those keys are therefore a wire format —
renaming one silently disables every webhook subscribed to it — which is why
:data:`EVENT_CATALOG` exists rather than the names living only in a docstring.
There are no receivers in this issue.

**Payloads carry ids only**: workspace id, contact id and whatever
event-specific id the event is about. No names, no field values, no message
bodies. An outbound webhook must not become a PII egress path, and a consumer
that needs the name has database access.

Two payload keys go slightly beyond the written contract, both deliberately and
both additive (Django receivers take ``**kwargs``, so a consumer coded against
the bare contract keeps working): ``source`` on ``contact.created`` is a fixed
vocabulary constant mirroring SPEC §11.8's ``opt_in_source``, and ``cleared`` on
``contact.field_changed`` says whether a value was set or removed. Neither is a
body — one is a category, the other the shape of a change rather than its value.

Named ``events.py`` rather than ``signals.py`` on purpose: in this repo
``signals.py`` is the *receiver* module (``apps/accounts/signals.py`` holds
``@receiver``s and is imported from ``AppConfig.ready()``). An emitter module
called ``signals.py`` invites a ``ready()`` import this app must not have.

**Sent synchronously, inside the caller's transaction** — not through
``transaction.on_commit``. Three reasons, in order of weight:

1. ``on_commit`` callbacks never run under ``pytest.mark.django_db``, because
   the suite's transaction never commits. Every test would have to remember
   ``django_capture_on_commit_callbacks``, and "works only when remembered" is
   precisely the failure mode this codebase keeps designing out — see the
   ``_ADMIN_ONLY_KEYS`` subtraction, the ``common.E004`` check, and the IDOR
   sweep's refusal to skip a route.
2. Every contract-7 consumer terminates in a ``scheduled_action`` insert in the
   same database, so sending inside the transaction makes the side effect atomic
   with its cause. An event that fires for a write that then rolls back is
   strictly worse than one that fires a few milliseconds early.
3. The decision belongs where the knowledge is. A receiver whose side effect
   escapes the database wraps *itself* in ``transaction.on_commit`` — only that
   receiver knows whether its work is externally visible.

``send()``, not ``send_robust()``: ``send_robust`` collects receiver exceptions
into a list nobody inspects, so a broken subscriber becomes a silently dropped
event. A receiver that must not break its emitter owns that guarantee.
"""

from typing import Any
from uuid import UUID

from django.dispatch import Signal
from django.utils import timezone

from apps.contacts.models import Contact

__all__ = [
    "CONTACT_EVENT_NAMES",
    "EVENT_CATALOG",
    "EVENT_CONTACT_CREATED",
    "EVENT_CONTACT_FIELD_CHANGED",
    "EVENT_CONTACT_TAG_ADDED",
    "EVENT_CONTACT_TAG_REMOVED",
    "contact_created",
    "contact_field_changed",
    "contact_tag_added",
    "contact_tag_removed",
    "emit",
]

EVENT_CONTACT_CREATED = "contact.created"
EVENT_CONTACT_TAG_ADDED = "contact.tag_added"
EVENT_CONTACT_TAG_REMOVED = "contact.tag_removed"
EVENT_CONTACT_FIELD_CHANGED = "contact.field_changed"

#: kwargs: ``workspace_id``, ``contact_id``, ``source``.
contact_created = Signal()

#: kwargs: ``workspace_id``, ``contact_id``, ``tag_id``.
contact_tag_added = Signal()

#: kwargs: ``workspace_id``, ``contact_id``, ``tag_id``.
contact_tag_removed = Signal()

#: kwargs: ``workspace_id``, ``contact_id``, ``field_id``, ``cleared``. Sent for
#: a set and for a clear alike; the payload says which field changed, never what
#: it changed to.
contact_field_changed = Signal()

EVENT_CATALOG: dict[str, Signal] = {
    EVENT_CONTACT_CREATED: contact_created,
    EVENT_CONTACT_TAG_ADDED: contact_tag_added,
    EVENT_CONTACT_TAG_REMOVED: contact_tag_removed,
    EVENT_CONTACT_FIELD_CHANGED: contact_field_changed,
}

#: For issue #25's ``outbound_webhook.events`` choices. This app's four only —
#: contract 7's catalog spans apps (``message.received`` is issue #8's,
#: ``broadcast.finished`` is #23's), so #25 unions the per-app catalogs. There is
#: deliberately no cross-app registry here for four strings.
CONTACT_EVENT_NAMES: tuple[str, ...] = tuple(EVENT_CATALOG)


def emit(event: str, *, workspace_id: UUID, contact_id: UUID, **extra: Any) -> None:
    """The one place an event leaves this app.

    A ``KeyError`` on an unknown name is the point: a mistyped event name has to
    be a crash, not a send that nobody receives.

    ``event`` is in the payload as well as being the catalog key, so issue #25
    can bind one receiver to all four and dispatch on the string. ``sender`` is
    ``Contact`` for every event and is therefore useless for discrimination.
    """
    EVENT_CATALOG[event].send(
        sender=Contact,
        event=event,
        workspace_id=workspace_id,
        contact_id=contact_id,
        occurred_at=timezone.now(),
        **extra,
    )
