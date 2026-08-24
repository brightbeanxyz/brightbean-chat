"""This app's half of the internal event catalog (ROADMAP contract 7).

Two events ship here: ``sequence.subscribed`` and ``sequence.unsubscribed``.
Contract 7 names both and assigns them to this issue, and two consumers pick
them up with no edit of their own:

* ``apps/api/events.py::discover_catalog`` walks the installed apps looking for
  an ``events`` module with an ``EVENT_CATALOG`` and connects its delivery
  receiver to everything it finds, so L5-F's outbound webhooks start delivering
  the day this module lands;
* :mod:`apps.campaigns.rules` binds SPEC §10's ``sequence_subscribed`` /
  ``sequence_unsubscribed`` rule triggers to these same two signals.

Every rule ``apps/contacts/events.py`` fixed for the first four events binds
these two, and each has a failure mode worth restating:

* **Dotted names are a wire format.** They appear in ``outbound_webhook.events``
  (SPEC §5), so renaming one silently disables every endpoint subscribed to it.
* **Ids only.** ``workspace_id``, ``contact_id``, ``sequence_id``,
  ``enrollment_id``. No sequence name, no step content, no message body — an
  outbound webhook must not become a PII egress path, and
  ``apps/api/events.py::publishable`` forwards ``*_id`` fields automatically for
  exactly this shape.
* **One ``emit()`` chokepoint** over a name→Signal dict, so a mistyped event
  name is a ``KeyError`` rather than a silent no-send.
* **Sent synchronously, inside the caller's transaction** — not through
  ``transaction.on_commit``, which never runs under ``pytest.mark.django_db``.
  Both consumers terminate in a row in the same database, so sending inside the
  transaction makes the side effect atomic with its cause. A receiver whose
  side effect escapes the database wraps *itself*.
* **``send()``, not ``send_robust()``.** ``send_robust`` collects receiver
  exceptions into a list nobody inspects, which turns a broken subscriber into a
  silently dropped event.

Named ``events.py`` because it *emits*; in this repo ``signals.py`` is the
receiver module, and an emitter with that name invites a ``ready()`` import.
"""

from typing import Any
from uuid import UUID

from django.dispatch import Signal
from django.utils import timezone

from apps.campaigns.models import Sequence

__all__ = [
    "CAMPAIGN_EVENT_NAMES",
    "EVENT_CATALOG",
    "EVENT_SEQUENCE_SUBSCRIBED",
    "EVENT_SEQUENCE_UNSUBSCRIBED",
    "emit",
    "sequence_subscribed",
    "sequence_unsubscribed",
]

EVENT_SEQUENCE_SUBSCRIBED = "sequence.subscribed"
EVENT_SEQUENCE_UNSUBSCRIBED = "sequence.unsubscribed"

#: kwargs: ``workspace_id``, ``contact_id``, ``sequence_id``, ``enrollment_id``.
sequence_subscribed = Signal()

#: kwargs: ``workspace_id``, ``contact_id``, ``sequence_id``, ``enrollment_id``.
#: Sent for a manual unsubscribe, an ``unsubscribe_sequence`` verb and the
#: retirement half of a re-enrollment alike — "this enrollment stopped early" is
#: one fact however it was caused. Completion is **not** one of them: a sequence
#: that ran to its last step did not unsubscribe anybody.
sequence_unsubscribed = Signal()

EVENT_CATALOG: dict[str, Signal] = {
    EVENT_SEQUENCE_SUBSCRIBED: sequence_subscribed,
    EVENT_SEQUENCE_UNSUBSCRIBED: sequence_unsubscribed,
}

#: This app's own two. Contract 7's catalog spans apps and ``apps.api`` unions
#: the per-app dicts by discovery; there is deliberately no cross-app registry
#: here for a pair of strings.
CAMPAIGN_EVENT_NAMES: tuple[str, ...] = tuple(EVENT_CATALOG)


def emit(event: str, *, workspace_id: UUID, contact_id: UUID, **extra: Any) -> None:
    """The one place an event leaves this app.

    A ``KeyError`` on an unknown name is the point: a mistyped event name has to
    be a crash, not a send nobody receives.

    ``event`` travels in the payload as well as being the catalog key, so a
    receiver bound to the whole of contract 7 discriminates on the string —
    ``sender`` differs per app and is useless for telling the events apart.
    """
    EVENT_CATALOG[event].send(
        sender=Sequence,
        event=event,
        workspace_id=workspace_id,
        contact_id=contact_id,
        occurred_at=timezone.now(),
        **extra,
    )
