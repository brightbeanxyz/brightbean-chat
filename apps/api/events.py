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
change is needed when it appears" true — and it has now been paid out once:
``apps/broadcasts/events.py`` (issue #23) landed with an ``EVENT_CATALOG`` and
its event started being delivered with nothing in this module edited. The next
one does the same.

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
    "PUBLISHABLE_FIELDS",
    "SUBSCRIBABLE_EVENTS",
    "connect_catalog_receivers",
    "discover_catalog",
    "publishable",
]

LOG = logging.getLogger(__name__)

_DISPATCH_UID = "apps.api.events.on_catalog_event"

#: The events an operator may subscribe an endpoint to, fixed by SPEC §5's
#: ``outbound_webhook.events`` column. This is the *offered* set, which is not
#: the same as the *emitted* set: an event may be offered before anything emits
#: it, in which case subscribing is a no-op rather than an error, and
#: ``broadcast.finished`` was that case until issue #23. Keeping
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

#: Non-id payload fields this app is willing to forward to a third party.
#:
#: Contract 7 promises its payloads carry "workspace id, contact id, and
#: event-specific ids only (no message bodies)", and everything below is one of
#: the handful of non-id values the current emitters add. The rule
#: :func:`publishable` applies is *ids plus this set* rather than "everything
#: except the envelope", because the denylist form publishes whatever a future
#: emitter happens to add — an outbound surface should widen deliberately, not
#: by side effect. Anything ending in ``_id`` still passes, so an emitter that
#: landed later (``broadcast.finished``'s ``broadcast_id``) needed no change
#: here, which is the property SPEC §5 asks for.
PUBLISHABLE_FIELDS: frozenset[str] = frozenset({"source", "platform", "preview", "cleared"})

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


def publishable(key: str) -> bool:
    """Whether a catalog payload field may be forwarded to a receiver.

    Ids always may; ``event``, ``workspace_id``, ``occurred_at`` and Django's
    own ``signal`` are the envelope and are carried separately; everything else
    has to be named in :data:`PUBLISHABLE_FIELDS`. So a payload field added
    upstream reaches third parties only if someone put it there on purpose.
    """
    if key in {"event", "workspace_id", "occurred_at", "signal"}:
        return False
    return key.endswith("_id") or key in PUBLISHABLE_FIELDS


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

    if event not in SUBSCRIBABLE_EVENTS:
        # Contract 7's catalog is wider than SPEC §5's subscribable set —
        # `contact.tag_removed` and `contact.field_changed` are emitted on every
        # field write and can never match a subscription, because
        # `services._validated_events` will not store them. Leaving before the
        # query means a 10k-row import does not pay for 10k lookups that cannot
        # return anything.
        return

    endpoints = OutboundWebhook.objects.for_workspace(workspace_id).filter(enabled=True).select_related("workspace")
    data = {key: value for key, value in payload.items() if publishable(key)}
    occurred_at = payload.get("occurred_at")
    for endpoint in endpoints:
        if event not in set(endpoint.events or ()):
            continue
        enqueue_delivery(endpoint, event=event, data=data, occurred_at=occurred_at)
