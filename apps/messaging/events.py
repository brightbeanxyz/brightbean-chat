"""This app's half of the internal event catalog (ROADMAP contract 7).

One event ships here: ``message.received``. Its consumers are L6-A's rule
triggers and L5-F's outbound webhooks, neither of which exists yet — an emitter
with no subscribers is the point, because the catalog is what lets those
workstreams be written without reading this app's internals.

Every rule ``apps/contacts/events.py`` fixed for the first four events binds
this one too, and they are worth restating because each has a failure mode:

* **Dotted names are a wire format.** ``message.received`` appears in
  ``outbound_webhook.events`` (SPEC §5), so renaming it breaks configured
  integrations in a way no test in this repository would notice.
* **Ids only.** No message body, no text, no username. An outbound webhook
  delivers this payload to a third-party URL, and a subscriber that wants
  content fetches it through the API with its own credentials.
* **One ``emit()`` chokepoint** over a name→Signal dict, so a mistyped event
  name is a ``KeyError`` rather than a silent no-send.
* **Sent synchronously, inside the caller's transaction** — not via
  ``transaction.on_commit``, which never runs under ``pytest.mark.django_db``.
  A receiver whose side effect escapes the database wraps *itself*.
* **``send()``, not ``send_robust()``.** A receiver that raises should fail the
  ingest transaction loudly rather than being swallowed into a log line.

The module is named ``events.py`` because it *emits*. ``signals.py`` is reserved
for receiver modules, which are imported from ``AppConfig.ready()``; an emitter
with that name invites a ``ready()`` import this app must not have.
"""

from typing import Any
from uuid import UUID

from django.dispatch import Signal
from django.utils import timezone

from apps.messaging.models import Message

__all__ = [
    "EVENT_CATALOG",
    "EVENT_MESSAGE_RECEIVED",
    "MESSAGING_EVENT_NAMES",
    "emit",
    "message_received",
]

EVENT_MESSAGE_RECEIVED = "message.received"

#: kwargs: ``workspace_id``, ``contact_id``, ``conversation_id``,
#: ``message_id``, ``connection_id``, ``platform``, ``occurred_at``.
message_received = Signal()

EVENT_CATALOG: dict[str, Signal] = {
    EVENT_MESSAGE_RECEIVED: message_received,
}

#: For issue #25's ``outbound_webhook.events`` choices. This app's own events
#: only — contract 7's catalog spans apps, so #25 unions the per-app tuples.
MESSAGING_EVENT_NAMES: tuple[str, ...] = tuple(EVENT_CATALOG)


def emit(event: str, *, workspace_id: UUID, contact_id: UUID, **extra: Any) -> None:
    """The one place an event leaves this app.

    Every send also carries ``event`` in its payload: a receiver bound to the
    whole of contract 7 discriminates on that string, because ``sender`` differs
    per app and is useless for telling the events apart.
    """
    EVENT_CATALOG[event].send(
        sender=Message,
        event=event,
        workspace_id=workspace_id,
        contact_id=contact_id,
        occurred_at=timezone.now(),
        **extra,
    )
