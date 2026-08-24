"""This app's half of the internal event catalog (ROADMAP contract 7).

One event ships here: ``broadcast.finished``, the one SPEC §5 lists among
``outbound_webhook.events`` and ``apps/api/events.py`` has been offering to
subscribers with nothing behind it. ``apps.api.events.discover_catalog`` walks
the installed apps looking for exactly this module and this dict, so the day it
lands its event starts being delivered — **with no edit to apps/api/**, which is
the property the Layer-6 gate checks.

Every rule ``apps/contacts/events.py`` and ``apps/messaging/events.py`` fixed
binds this one too:

* **Dotted names are a wire format.** ``broadcast.finished`` is already storable
  in operators' ``outbound_webhook.events`` rows, so renaming it breaks
  configured integrations in a way no test here would notice.
* **Ids only.** No audience, no counts, no message content.
  ``apps.api.events.publishable`` would drop them anyway — it forwards ``*_id``
  plus a named allowlist — and sending a subscriber a number it then has to
  reconcile against the API is worse than sending it nothing.
* **One ``emit()`` chokepoint** over a name→Signal dict, so a mistyped event name
  is a ``KeyError`` rather than a silent no-send.
* **Sent synchronously, inside the caller's transaction.** The settle that emits
  this runs in one; if it rolls back, so does the queued delivery row, and no
  subscriber hears about a broadcast that did not finish.
* **``send()``, not ``send_robust()``.** A receiver that raises should fail the
  settle loudly rather than being swallowed into a log line.

Named ``events.py`` because it *emits*; ``signals.py`` is reserved for receiver
modules, which are imported from ``AppConfig.ready()``.
"""

from typing import Any
from uuid import UUID

from django.dispatch import Signal
from django.utils import timezone

__all__ = [
    "BROADCAST_EVENT_NAMES",
    "EVENT_BROADCAST_FINISHED",
    "EVENT_CATALOG",
    "broadcast_finished",
    "emit",
]

EVENT_BROADCAST_FINISHED = "broadcast.finished"

#: kwargs: ``workspace_id``, ``broadcast_id``, ``occurred_at``.
broadcast_finished = Signal()

EVENT_CATALOG: dict[str, Signal] = {
    EVENT_BROADCAST_FINISHED: broadcast_finished,
}

#: This app's own event names. Contract 7's catalog spans apps; #25 unions them.
BROADCAST_EVENT_NAMES: tuple[str, ...] = tuple(EVENT_CATALOG)


def emit(event: str, *, workspace_id: UUID, broadcast_id: UUID, **extra: Any) -> None:
    """The one place an event leaves this app.

    ``sender`` is this app's model class, and it is deliberately not what a
    receiver discriminates on — every payload carries ``event`` as well, because
    ``apps.api.events.on_catalog_event`` binds to the whole of contract 7 and
    dispatches on that string.

    No ``contact_id``: a broadcast finishing is not about one person, and
    contract 7 fixes only ``workspace_id`` plus the event's own ids.
    """
    from apps.broadcasts.models import Broadcast

    EVENT_CATALOG[event].send(
        sender=Broadcast,
        event=event,
        workspace_id=workspace_id,
        broadcast_id=broadcast_id,
        occurred_at=timezone.now(),
        **extra,
    )
