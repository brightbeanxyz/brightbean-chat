"""Subscribing to the internal event catalog (ROADMAP contract 7).

Contract 7 spreads its catalog across apps on purpose — ``apps/contacts/events.py``,
``apps/messaging/events.py`` and ``apps/flows/events.py`` each own an
``EVENT_CATALOG`` and each says, in as many words, that issue #25 unions them
and that there is deliberately no cross-app registry for a handful of strings.

So this module unions them by **discovery** rather than by a list. It walks the
installed apps looking for an ``events`` module with an ``EVENT_CATALOG`` and
connects one receiver to every signal it finds. That is not tidiness: SPEC §5
lists ``broadcast.finished`` among the subscribable events and L6-B is what
emits it. Discovery is what makes "the subscription is data-driven, so no code
change is needed when it appears" true — the day ``apps/campaigns/events.py``
lands with an ``EVENT_CATALOG``, its events start being delivered.

Every payload in the catalog carries ``event`` as well as being keyed by it,
precisely so one receiver can bind to all of them and dispatch on the string.

**Nothing here imports** ``apps.api.models`` **at module scope** — models.py
reads :data:`SUBSCRIBABLE_EVENTS` from here, and the cycle would be immediate.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
from typing import Any

from django.apps import apps as django_apps
from django.dispatch import Signal

__all__ = [
    "SUBSCRIBABLE_EVENTS",
    "connect_catalog_receivers",
    "discover_catalog",
]

LOG = logging.getLogger(__name__)

_DISPATCH_UID = "apps.api.events.on_catalog_event"

#: The events an operator may subscribe an endpoint to, fixed by SPEC §5's
#: ``outbound_webhook.events`` column. This is the *offered* set, which is not
#: the same as the *emitted* set: ``broadcast.finished`` has no emitter until
#: L6-B, and subscribing to it today is a no-op rather than an error. Keeping
#: the list here rather than deriving it from :func:`discover_catalog` means the
#: settings UI offers a stable set of checkboxes that does not change shape
#: depending on which apps happen to be installed.
SUBSCRIBABLE_EVENTS: tuple[str, ...] = (
    "contact.created",
    "contact.tag_added",
    "message.received",
    "execution.completed",
    "broadcast.finished",
)

#: Human copy for the settings page, kept beside the names so a new event
#: cannot be offered without one.
EVENT_LABELS: dict[str, str] = {
    "contact.created": "Contact created",
    "contact.tag_added": "Tag added to a contact",
    "message.received": "Inbound message received",
    "execution.completed": "Flow execution completed",
    "broadcast.finished": "Broadcast finished",
}


def discover_catalog() -> dict[str, Signal]:
    """Every catalog event this deployment can emit, mapped to its signal.

    Only modules that genuinely do not exist are skipped. An ``events`` module
    that exists but fails to import is a real bug and is allowed to raise —
    swallowing it would turn a broken emitter into silently undelivered
    webhooks, which is the kind of failure nobody notices for a week.
    """
    catalog: dict[str, Signal] = {}
    for config in django_apps.get_app_configs():
        module_name = f"{config.name}.events"
        if module_name == __name__:
            continue
        try:
            if importlib.util.find_spec(module_name) is None:
                continue
        except ModuleNotFoundError:
            continue
        module = importlib.import_module(module_name)
        found = getattr(module, "EVENT_CATALOG", None)
        if not isinstance(found, dict):
            continue
        for name, signal in found.items():
            if isinstance(signal, Signal):
                catalog[name] = signal
    return catalog


def connect_catalog_receivers() -> dict[str, Signal]:
    """Bind the delivery receiver to every discovered catalog signal.

    Idempotent through ``dispatch_uid``, so a second ``ready()`` in the same
    process — which the test suite does whenever it reloads app configs — does
    not double every delivery.
    """
    catalog = discover_catalog()
    for signal in catalog.values():
        signal.connect(on_catalog_event, dispatch_uid=_DISPATCH_UID, weak=False)
    return catalog


def on_catalog_event(sender: Any = None, **payload: Any) -> None:
    """Fan one catalog event out to the workspace's subscribed endpoints.

    Runs synchronously inside the emitting transaction — the catalog modules are
    explicit that they use ``send()`` rather than ``send_robust()`` and do not
    wait for commit. So this does the cheap half here (one indexed query for
    matching endpoints, one queued row each) and leaves the network call to the
    worker. If the emitter rolls back, the queued rows roll back with it, which
    is exactly the behaviour a webhook consumer wants: no delivery announcing a
    change that did not happen.
    """
    from apps.api.delivery import enqueue_delivery
    from apps.api.models import OutboundWebhook

    event = payload.get("event")
    workspace_id = payload.get("workspace_id")
    if not event or not workspace_id:
        # Contract 7 fixes both fields on every payload; a signal without them
        # is not ours to interpret.
        return

    endpoints = OutboundWebhook.objects.for_workspace(workspace_id).filter(enabled=True).select_related("workspace")
    data = {
        key: value for key, value in payload.items() if key not in {"event", "workspace_id", "signal", "occurred_at"}
    }
    occurred_at = payload.get("occurred_at")
    for endpoint in endpoints:
        if event not in set(endpoint.events or ()):
            continue
        enqueue_delivery(endpoint, event=event, data=data, occurred_at=occurred_at)
