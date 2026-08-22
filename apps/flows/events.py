"""The flow half of the internal event catalog (ROADMAP contract 7).

One signal with a fixed dotted name, ``execution.completed``. Two later
workstreams consume it and neither exists yet: issue #22 (L6-A) turns catalog
events into rule triggers, and issue #25 (L5-F) fans them out as outbound
webhooks, storing the dotted string in ``outbound_webhook.events`` (SPEC §5).
The key is therefore a wire format — renaming it silently disables every webhook
subscribed to it.

Modelled on :mod:`apps.contacts.events` deliberately, down to the module name:
the two are the same contract seen from two apps, and a consumer that has to
learn a different shape per emitter is a consumer that will get one of them
wrong. That module's reasoning applies unchanged and is not repeated here —
ids-only payloads, ``send()`` rather than ``send_robust()``, and emission inside
the caller's transaction rather than through ``transaction.on_commit``.

**Only completion is an event.** A run that fails or expires does not emit; it
raises an in-app notification instead (``flow_execution_failed``,
``flow_loop_cap_hit``), because those are things a human has to act on rather
than facts a downstream system wants to observe. Contract 7 names exactly
``execution.completed``, and widening a wire format to mean "reached any
terminal state" would break the meaning of every subscription already written
against the name.
"""

from typing import Any
from uuid import UUID

from django.dispatch import Signal
from django.utils import timezone

__all__ = [
    "EVENT_CATALOG",
    "EVENT_EXECUTION_COMPLETED",
    "FLOW_EVENT_NAMES",
    "emit",
    "execution_completed",
]

EVENT_EXECUTION_COMPLETED = "execution.completed"

#: kwargs: ``workspace_id``, ``contact_id``, ``execution_id``, ``flow_id``,
#: ``flow_version_id``, ``preview``, ``occurred_at``.
#:
#: ``preview`` rides along because a draft-preview run (SPEC §16) is a real
#: execution that a subscriber almost certainly does not want to react to, and
#: a consumer cannot tell from ids alone. It is a flag, not a body.
execution_completed = Signal()

EVENT_CATALOG: dict[str, Signal] = {EVENT_EXECUTION_COMPLETED: execution_completed}

#: For issue #25's ``outbound_webhook.events`` choices. This app's own only;
#: contract 7's catalog spans apps and #25 unions the per-app tuples.
FLOW_EVENT_NAMES: tuple[str, ...] = tuple(EVENT_CATALOG)


def emit(event: str, *, workspace_id: UUID, contact_id: UUID, **extra: Any) -> None:
    """The one place an event leaves this app.

    A ``KeyError`` on an unknown name is the point: a mistyped event name has to
    be a crash, not a send nobody receives.
    """
    from apps.flows.models import FlowExecution

    EVENT_CATALOG[event].send(
        sender=FlowExecution,
        event=event,
        workspace_id=workspace_id,
        contact_id=contact_id,
        occurred_at=timezone.now(),
        **extra,
    )
