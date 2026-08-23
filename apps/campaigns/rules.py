"""SPEC §10's ``rule`` trigger, bound to the internal event catalog.

**Rule triggers are event-bus consumers, not routing-hook registrants.** ROADMAP
contract 6 says so in as many words — "L6-A's rule triggers consume the event
catalog (contract 7), not this pipeline" — and the difference is not cosmetic. A
routing hook runs when a *message arrives on a channel*; a rule fires when
*something happens to a contact*, which is often nothing to do with a message
and frequently has no channel at all. So there is no ``register_hook`` call in
this app, and a test asserts there never will be.

The six events SPEC §10 lists map onto four contact signals and this app's two:

===========================  ==============================
SPEC §10 ``event``           catalog signal
===========================  ==============================
``contact_created``          ``contact.created``
``tag_added``                ``contact.tag_added``
``tag_removed``              ``contact.tag_removed``
``field_changed``            ``contact.field_changed``
``sequence_subscribed``      ``sequence.subscribed``
``sequence_unsubscribed``    ``sequence.unsubscribed``
===========================  ==============================

--------------------------------------------------------------------------
Two halves: a cheap receiver, and the work in the worker
--------------------------------------------------------------------------

:func:`on_rule_event` is on the emitter's hot path — it runs inside whatever
transaction wrote the tag, and that write is very often one of thousands. So it
does one indexed lookup and, if this workspace has any rule trigger for the
event, queues one row. :func:`match_and_fire` does everything else from the
queue handler. That is the same split ``apps/api/events.py::on_catalog_event``
makes under the same constraint, and its reasoning applies verbatim: the cheap
half in the transaction, the expensive half where a worker can take its time.

Three filters, applied in that order and each cheaper than the next:

1. the config's ``event`` must be the one that fired;
2. ``tag_id`` / ``field_id``, when set, must equal the id **in the payload** —
   that is what makes "when the VIP tag is added" mean the tag that was just
   added, rather than "when any tag is added to somebody who has VIP", which is
   all a contact-level filter can express. Only the filter belonging to the
   event that fired is consulted (:data:`_ID_FILTER_FOR`): the schema permits
   either key on any rule, and comparing one against a payload that never
   carries it would make the rule silently unfireable;
3. ``filters``, a SPEC §11.4 condition document, evaluated against the contact
   *after* the change. "Field changed, and its new value is gold" is this one.

--------------------------------------------------------------------------
Loop safety
--------------------------------------------------------------------------

A rule whose flow causes the event that triggered it is a loop, and there are
three independent things standing in its way. Worth knowing all three, because
each closes a case the others do not:

* **Transition semantics in the emitter.** ``contacts.services.add_tag`` sends
  ``contact.tag_added`` only when the link is genuinely new, so "rule adds the
  tag that triggered it" terminates after one pass on its own.
* **This module's 60-second cooldown**, per (trigger, contact). It is what
  catches the case transition semantics cannot: a flow that *removes* a tag and
  re-adds it produces a real transition every time. Rolling rather than
  clock-aligned, for the reason
  ``apps/flows/triggers/guards.py::claim_default_reply`` gives — a fixed window
  lets two fires seconds apart both through at a boundary, which is exactly the
  case a loop breaker exists for.
* **The engine's own limits**: the loop cap on blocks per execution, and the
  partial unique index that allows one live execution per (contact, flow).

The cooldown is claimed **after** matching, so an event that was never going to
fire a trigger does not consume its budget, and a trigger that is cooling down
stops the pass rather than handing the event to the next candidate — SPEC §10's
first-match-wins is about which trigger owns the event, and a suppressed fire
does not change the owner.
"""

import logging
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.campaigns.events import (
    EVENT_CATALOG as CAMPAIGN_CATALOG,
)
from apps.campaigns.events import (
    EVENT_SEQUENCE_SUBSCRIBED,
    EVENT_SEQUENCE_UNSUBSCRIBED,
)
from apps.campaigns.models import RuleTriggerFire
from apps.contacts.conditions import ConditionError, evaluate
from apps.contacts.events import (
    EVENT_CATALOG as CONTACT_CATALOG,
)
from apps.contacts.events import (
    EVENT_CONTACT_CREATED,
    EVENT_CONTACT_FIELD_CHANGED,
    EVENT_CONTACT_TAG_ADDED,
    EVENT_CONTACT_TAG_REMOVED,
)
from apps.contacts.models import Contact, ContactStatus
from apps.queueing.registry import schedule

__all__ = [
    "ACTION_RULE_EVENT",
    "COOLDOWN",
    "RULE_EVENT_FOR",
    "claim_rule_fire",
    "connect_rule_receivers",
    "match_and_fire",
    "on_rule_event",
]

logger = logging.getLogger(__name__)

#: SPEC §10's rule-trigger cooldown, per contact per trigger.
COOLDOWN = timedelta(seconds=60)

#: The queue type this receiver defers to. A plain string rather than an
#: ``ActionType`` member: that enum is deliberately not attached to the column as
#: ``choices``, so a new type is a handler registration and never an ``AlterField``
#: in ``apps.queueing`` — the same way ``route_event`` and ``webhook_delivery``
#: were added by their own apps.
ACTION_RULE_EVENT = "rule_event"

_DISPATCH_UID = "apps.campaigns.rules.on_rule_event"

#: Catalog event name -> the ``event`` word a rule trigger's config stores.
#: ``apps/flows/triggers/schema.py::RULE`` fixes the right-hand column and
#: ``apps/flows/views_triggers.py::_RULE_EVENTS`` renders it, so this table is
#: the only place the two vocabularies meet.
RULE_EVENT_FOR: dict[str, str] = {
    EVENT_CONTACT_CREATED: "contact_created",
    EVENT_CONTACT_TAG_ADDED: "tag_added",
    EVENT_CONTACT_TAG_REMOVED: "tag_removed",
    EVENT_CONTACT_FIELD_CHANGED: "field_changed",
    EVENT_SEQUENCE_SUBSCRIBED: "sequence_subscribed",
    EVENT_SEQUENCE_UNSUBSCRIBED: "sequence_unsubscribed",
}

#: Which config key filters which event, and which payload id it is compared
#: against. Keyed by event, deliberately: the schema permits ``tag_id`` and
#: ``field_id`` on any rule, so a config that carries one the event does not
#: report — written by the public API, a flow import, or a client that changed
#: the event without clearing the old key — would otherwise compare against a
#: missing payload id and the rule would silently never fire.
_ID_FILTER_FOR: dict[str, tuple[str, str]] = {
    "tag_added": ("tag_id", "tag_id"),
    "tag_removed": ("tag_id", "tag_id"),
    "field_changed": ("field_id", "field_id"),
}


def connect_rule_receivers() -> None:
    """Bind the receiver to every catalog signal a rule can name.

    Only those signals. ``message.received`` and ``execution.completed`` are in
    contract 7's catalog and are not in SPEC §10's rule vocabulary, so binding
    to the whole catalog would put this receiver on the ingest hot path for
    events it can only ever decline.

    Idempotent through ``dispatch_uid``: ``ready()`` runs twice under some
    autoreload paths, and the test suite reloads app configs.
    """
    for name in RULE_EVENT_FOR:
        signal = CONTACT_CATALOG.get(name) or CAMPAIGN_CATALOG.get(name)
        if signal is not None:
            signal.connect(on_rule_event, dispatch_uid=_DISPATCH_UID, weak=False)


def on_rule_event(sender: Any = None, **payload: Any) -> None:
    """Queue the rule-trigger pass for this event. Does **not** run a flow.

    This receiver is on the emitter's hot path: it runs synchronously inside
    whatever transaction wrote the tag, the field or the contact, and that write
    is very often one of thousands — ``contacts.imports`` calls ``create_contact``
    per CSV row and ``contacts.bulk_tag`` tags up to 500 contacts per request.
    Starting a flow here would put a whole graph execution, sends included,
    inside each of those transactions.

    So it does what ``apps/api/events.py::on_catalog_event`` does with the same
    constraint — "the cheap half here ... and leaves the network call to the
    worker": one indexed query to find out whether this workspace has any rule
    trigger for this event at all, and, only if it does, one queued row. The
    matching, the filters, the cooldown and the flow start all happen in
    :func:`apps.campaigns.handlers.handle_rule_event`.

    Queuing inside the transaction keeps the property that matters: if the tag
    write rolls back, the queued row rolls back with it, so no run is ever
    announced for a change that did not happen.
    """
    event = RULE_EVENT_FOR.get(str(payload.get("event") or ""))
    workspace_id = payload.get("workspace_id")
    contact_id = payload.get("contact_id")
    if event is None or not workspace_id or not contact_id:
        return

    # `.first()` rather than `.exists()`: the same one indexed query, and it
    # hands back the workspace instance `schedule()` needs — which would
    # otherwise be a second lookup on the path that is about to queue anyway.
    candidate = _candidates(workspace_id, event).select_related("workspace").first()
    if candidate is None:
        # The common case by a wide margin — a workspace with no rule trigger for
        # this event pays one indexed query per contact write and nothing else.
        return

    schedule(
        ACTION_RULE_EVENT,
        timezone.now(),
        _queued_payload(event, payload),
        workspace=candidate.workspace,
        contact=contact_id,
    )


def _queued_payload(event: str, payload: dict[str, Any]) -> dict[str, Any]:
    """The event, reduced to what the handler needs and to what JSON can hold.

    Ids only, plus the rule event name. That is not merely convenient — it is
    contract 7's own rule, and it means a queued row sitting in the table for a
    minute holds nothing about the contact beyond keys. ``occurred_at`` is a
    datetime and Django's signal envelope carries a ``signal`` object; neither is
    JSON, and neither is anything the matcher reads.
    """
    ids = {
        key: str(value)
        for key, value in payload.items()
        # `workspace_id` is left out on purpose: the row carries a `workspace`
        # column and the handler re-derives scope from *that*, so a copy in the
        # payload would be a tenancy claim a stale document could contradict.
        if key.endswith("_id") and value and key != "workspace_id"
    }
    return {"event": event, **ids}


def match_and_fire(event: str, payload: dict[str, Any]) -> None:
    """SPEC §10's match: first trigger in priority order that fires, wins.

    Called from the queue handler, which already holds the contact's advisory
    lock because the row names a contact — so the flow start below runs under
    the same discipline the webhook path uses.
    """
    workspace_id = payload.get("workspace_id")
    contact = _contact(workspace_id, payload.get("contact_id"))
    if contact is None:
        # Deleted between the emit and here, or soft-deleted: a tombstoned
        # contact must not be put back into a send path.
        return

    for trigger in _candidates(workspace_id, event):
        if not _matches(trigger, payload, contact, event):
            continue
        if not claim_rule_fire(trigger, contact):
            logger.info("Rule trigger %s is cooling down for contact %s; not firing.", trigger.pk, contact.pk)
            return
        _fire(trigger, contact, event=event, payload=payload)
        return


def _candidates(workspace_id: Any, event: str) -> Any:
    """Enabled rule triggers on runnable flows, in SPEC §10's match order.

    The ``Exists`` clause is not an optimisation, for the reason
    ``apps/flows/triggers/matching.py::candidates`` gives: a trigger pointing at
    a flow with no published version would otherwise win the match and then
    swallow the event, and the trigger that should have matched next never runs.

    ``Meta.ordering`` on ``Trigger`` is ``priority, created_at, id`` — already
    exactly SPEC §10's order, and load-bearing here because the first match wins.
    """
    from django.db.models import Exists, OuterRef

    from apps.flows.models import FlowStatus, FlowVersion, Trigger, TriggerType

    published = FlowVersion.objects.unscoped().filter(flow_id=OuterRef("flow_id"), published=True)
    # .unscoped() with a reason, per CONTRIBUTING.md: a correlated subquery whose
    # outer query is already scoped by for_workspace(), compiled into that query
    # rather than executed on its own.
    return (
        Trigger.objects.for_workspace(workspace_id)
        .filter(type=TriggerType.RULE, enabled=True, flow__status=FlowStatus.ACTIVE)
        .filter(Exists(published))
        .filter(config_json__event=event)
        .select_related("flow", "channel_connection")
    )


def _matches(trigger: Any, payload: dict[str, Any], contact: Contact, event: str) -> bool:
    """The two filters the candidate query could not express.

    Only this event's id filter is consulted; a stray key belonging to another
    event is ignored rather than treated as a condition nothing can satisfy.
    """
    config = trigger.config_json if isinstance(trigger.config_json, dict) else {}

    id_filter = _ID_FILTER_FOR.get(event)
    if id_filter is not None:
        config_key, payload_key = id_filter
        wanted = config.get(config_key)
        if wanted and str(wanted) != str(payload.get(payload_key) or ""):
            return False

    filters = config.get("filters")
    if not filters:
        return True
    try:
        return evaluate(contact, filters)
    except ConditionError:
        # A filter naming a tag somebody has since deleted, or a source this
        # deployment cannot evaluate. Declining is the safe direction: firing a
        # flow because its audience filter failed to compile is how a campaign
        # reaches everybody.
        logger.warning("Rule trigger %s has a filter that cannot be evaluated; declining.", trigger.pk, exc_info=True)
        return False


def _contact(workspace_id: Any, contact_id: Any) -> Contact | None:
    try:
        pk = UUID(str(contact_id))
    except (ValueError, AttributeError, TypeError):
        return None
    return (
        Contact.objects.for_workspace(workspace_id)
        .filter(pk=pk, status=ContactStatus.ACTIVE)
        .select_related("workspace")
        .first()
    )


def _fire(trigger: Any, contact: Contact, *, event: str, payload: dict[str, Any]) -> None:
    """Hand the matched trigger to the shared non-webhook entry point.

    The variables carry the event name and whatever ids the payload held, so a
    flow can render ``{{tag_id}}`` and branch on what happened. Ids only — the
    catalog never carries anything else, and a renderer is the last place a
    message body should arrive from.
    """
    from apps.flows.triggers.entrypoints import fire_trigger

    variables = {"rule_event": event}
    # `workspace_id` is envelope rather than subject — every flow already runs in
    # one workspace, and putting it in the renderer's namespace is a value an
    # author can print into a message for no reason.
    variables.update(
        {key: str(value) for key, value in payload.items() if key.endswith("_id") and value and key != "workspace_id"}
    )

    result = fire_trigger(trigger=trigger, contact=contact, variables=variables)
    if not result.started:
        logger.info("Rule trigger %s did not start for contact %s: %s", trigger.pk, contact.pk, result.reason)


def claim_rule_fire(trigger: Any, contact: Contact, *, now: datetime | None = None) -> bool:
    """May this trigger fire for this contact now — and if so, take the guard.

    Rolling, not clock-aligned: the window restarts from the last fire, so there
    is no boundary at which two fires seconds apart are both allowed.

    Correct **without** the contact advisory lock, which matters because this
    runs inside whatever transaction emitted the event and that transaction's
    hold on the lock is a property of one code path rather than of this function.
    The ``UPDATE`` takes a row lock for its own duration, so of two concurrent
    claims exactly one sees ``updated == 1``; the very first claim for a pair is
    arbitrated by the unique constraint instead.
    """
    moment = now or timezone.now()
    updated = (
        RuleTriggerFire.objects.for_workspace(contact.workspace_id)
        .filter(trigger=trigger, contact=contact, last_fired_at__lte=moment - COOLDOWN)
        .update(last_fired_at=moment, updated_at=moment)
    )
    if updated:
        return True

    try:
        # Its own atomic block: an IntegrityError caught without one poisons the
        # transaction the emitter opened around this whole receiver.
        with transaction.atomic():
            RuleTriggerFire(trigger=trigger, contact=contact, last_fired_at=moment).save()
        return True
    except IntegrityError:
        # A row exists and is inside the window, or a concurrent claim won the
        # insert. Both mean the same thing to the caller.
        return False
