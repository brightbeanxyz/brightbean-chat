"""The doors into routing that are not a webhook.

Two callers today, and neither of them has a ``NormalizedEvent`` to route:

* SPEC §10's ``api`` trigger — "fired via public API flow-start endpoint", which
  #25 (L5-F) owns;
* SPEC §10's ``rule`` trigger, which L6-A fires from the internal event catalog
  (``apps/campaigns/rules.py``, ROADMAP contract 7).

Both need the same discipline around the contact lock, and :func:`fire_trigger`
is it. Neither matches anything here — by the time a caller arrives the trigger
is already chosen — so what is left to get right is the lock, and the fallback
when somebody else is holding it.
"""

import logging
from dataclasses import dataclass
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.flows.engine import FlowNotRunnableError, start_flow
from apps.flows.models import FlowExecution, StartedBy, Trigger
from apps.flows.triggers.services import api_trigger
from apps.queueing.models import ActionType
from apps.queueing.registry import schedule

__all__ = ["ApiTriggerResult", "connection_for_contact", "fire_api_trigger", "fire_trigger"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ApiTriggerResult:
    """What happened. Three outcomes, none of them an exception."""

    trigger: Trigger | None = None
    execution: FlowExecution | None = None
    scheduled: Any = None
    reason: str = ""

    @property
    def started(self) -> bool:
        return self.execution is not None or self.scheduled is not None


def fire_trigger(
    *,
    trigger: Trigger,
    contact: Any,
    variables: dict[str, Any] | None = None,
    connection: Any = None,
) -> ApiTriggerResult:
    """Start ``trigger``'s flow for ``contact``. The trigger is already matched.

    Takes the **non-blocking** contact lock and enqueues on contention. Both
    callers are inside somebody's request or somebody's transaction — an API
    call for one, a signal receiver inside the emitting write for the other — and
    waiting there behind whatever the worker is doing to this contact is a held
    connection rather than a slow one.

    ``START_FLOW`` rather than ``route_event`` on the fallback: there is no
    ``NormalizedEvent`` and no stage chain to replay, so the only thing worth
    deferring is the start itself.

    Every outcome is a result rather than an exception, including "the flow has
    no published version": a trigger that cannot run is a configuration problem,
    not a failure of the event that reached it.
    """
    from apps.queueing.locks import try_contact_lock

    flow = trigger.flow
    if contact.workspace_id != flow.workspace_id:
        return ApiTriggerResult(trigger=trigger, reason="cross_workspace")

    target = connection if connection is not None else _connection_for(trigger, contact)
    payload = {"trigger_type": trigger.type, **(variables or {})}

    with transaction.atomic(), try_contact_lock(contact) as acquired:
        if not acquired:
            action = schedule(
                ActionType.START_FLOW,
                timezone.now(),
                {
                    "contact_id": str(contact.pk),
                    "flow_id": str(flow.pk),
                    "connection_id": str(target.pk) if target is not None else None,
                    "variables": payload,
                    "started_by": StartedBy.stamp(StartedBy.TRIGGER, trigger.pk),
                },
                workspace=flow.workspace,
                contact=contact,
            )
            return ApiTriggerResult(trigger=trigger, scheduled=action, reason="lock_contention")

        try:
            execution = start_flow(
                contact,
                flow,
                started_by=StartedBy.stamp(StartedBy.TRIGGER, trigger.pk),
                variables=payload,
                connection=target,
            )
        except FlowNotRunnableError as exc:
            logger.warning("trigger %s cannot start flow %s: %s", trigger.pk, flow.pk, exc)
            return ApiTriggerResult(trigger=trigger, reason="not_runnable")

    return ApiTriggerResult(trigger=trigger, execution=execution)


def fire_api_trigger(
    *,
    flow: Any,
    contact: Any,
    key: str = "",
    variables: dict[str, Any] | None = None,
    connection: Any = None,
) -> ApiTriggerResult:
    """Start ``flow`` for ``contact`` through its ``api`` trigger.

    Resolves the trigger, then hands over to :func:`fire_trigger`; the lock
    discipline is shared rather than restated, so the API door and the rule door
    cannot drift apart.
    """
    if contact.workspace_id != flow.workspace_id:
        return ApiTriggerResult(reason="cross_workspace")

    trigger = api_trigger(flow, key=key)
    if trigger is None:
        return ApiTriggerResult(reason="no_api_trigger")

    return fire_trigger(trigger=trigger, contact=contact, variables=variables, connection=connection)


def _connection_for(trigger: Trigger, contact: Any) -> Any | None:
    """Which channel a trigger-started run happens on.

    The trigger's own binding if it has one, otherwise the contact's own most
    recent reachable identity.
    """
    if trigger.channel_connection_id is not None:
        return trigger.channel_connection
    return connection_for_contact(contact)


def connection_for_contact(contact: Any) -> Any | None:
    """The channel a run for ``contact`` should happen on when nothing names one.

    ``apps/flows/engine/sending.py`` leaves this open on purpose — inventing one
    there "would be the send path guessing at routing, which is L3-A's and
    L4-A's to decide". This is that decision, and it is shared with L6-A's
    sequence steps, which have no trigger to ask: the contact's most recently
    active identity that has not opted out. ``None`` is a legitimate answer; the
    send pipeline reports it.
    """
    from apps.flows.compat import installed_model

    model = installed_model("messaging", "apps.messaging", "ContactChannelIdentity")
    if model is None:  # pragma: no cover - messaging is installed everywhere
        return None
    identity = (
        model.objects.for_workspace(contact.workspace_id)
        .filter(contact=contact, opted_out_at__isnull=True, channel_connection__isnull=False)
        .order_by("-last_inbound_at", "-created_at")
        .select_related("channel_connection")
        .first()
    )
    return identity.channel_connection if identity is not None else None
