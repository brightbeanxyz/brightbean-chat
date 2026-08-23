"""The doors into routing that are not a webhook.

SPEC §10's ``api`` trigger is "fired via public API flow-start endpoint". #25
(L5-F) owns that endpoint; this is the function it calls, shipped now so the door
exists and is tested before its caller lands — and so #25 adds a route rather
than a second copy of the lock discipline.
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

__all__ = ["ApiTriggerResult", "fire_api_trigger"]

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


def fire_api_trigger(
    *,
    flow: Any,
    contact: Any,
    key: str = "",
    variables: dict[str, Any] | None = None,
    connection: Any = None,
) -> ApiTriggerResult:
    """Start ``flow`` for ``contact`` through its ``api`` trigger.

    Takes the **non-blocking** contact lock and enqueues on contention, for the
    same reason the webhook path does: this is a request too, and a caller
    waiting behind whatever the worker is doing to this contact is a held
    connection rather than a slow one.

    ``START_FLOW`` rather than ``route_event`` on the fallback: there is no
    ``NormalizedEvent`` and no stage chain to replay — the trigger is already
    matched, so the only thing worth deferring is the start itself.
    """
    from apps.queueing.locks import try_contact_lock

    if contact.workspace_id != flow.workspace_id:
        return ApiTriggerResult(reason="cross_workspace")

    trigger = api_trigger(flow, key=key)
    if trigger is None:
        return ApiTriggerResult(reason="no_api_trigger")

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
            logger.warning("api trigger %s cannot start flow %s: %s", trigger.pk, flow.pk, exc)
            return ApiTriggerResult(trigger=trigger, reason="not_runnable")

    return ApiTriggerResult(trigger=trigger, execution=execution)


def _connection_for(trigger: Trigger, contact: Any) -> Any | None:
    """Which channel an API-started run happens on.

    ``apps/flows/engine/sending.py`` leaves this open on purpose — inventing one
    there "would be the send path guessing at routing, which is L3-A's and
    L4-A's to decide". This is that decision: the trigger's own binding if it has
    one, otherwise the contact's most recently active identity that has not
    opted out. ``None`` is a legitimate answer; the send pipeline reports it.
    """
    if trigger.channel_connection_id is not None:
        return trigger.channel_connection

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
