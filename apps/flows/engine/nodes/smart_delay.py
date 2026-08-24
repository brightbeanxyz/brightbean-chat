"""SPEC §11.5 — wait until a moment, adjusted into the next allowed window.

    Config: mode duration {value, unit: minutes|hours|days} or date {field or
    fixed datetime}; continue_window {enabled, days[], from, to,
    use_contact_timezone}. Behavior: Schedule(run_at adjusted into the next
    allowed window). Handle: default.

The node computes one instant and returns ``Schedule``; the runner writes the
row and enqueues the wake-up. Nothing here sleeps, and nothing here is resumable
by an inbound event — SPEC §9.3 is explicit that a smart delay is "resumed only
by its scheduled_action", which is why the runner parks it as ``waiting_delay``
rather than ``waiting_reply``.

**Which clock.** ``use_contact_timezone`` picks between the contact's own
timezone and the workspace's (``Workspace.effective_timezone``). The contact's
is a free-text column populated from platform profiles, so an unparseable one
falls back rather than failing — a bad timezone string should delay a message,
not end a run.

**The window arithmetic is not here.** ``continue_window`` and a sequence step's
``send_window`` (SPEC §12, issue #22) are the same object computed the same way,
so :mod:`apps.common.windows` owns it and both call in. ``WEEKDAYS`` is
re-exported below because this module was its first home and tests import it
from here.

**A date-mode delay with nothing to compute from fails the run**, and that is
deliberate. "Wait until the renewal date" with an empty renewal date has two
possible readings, and the other one — treat it as *now* — sends the renewal
reminder immediately to everyone missing the field. A visible failure with an
in-app notification is the recoverable mistake; a burst of wrong messages is
not.
"""

import logging
from datetime import date, datetime, time, timedelta, tzinfo

from django.utils import timezone

from apps.common.windows import WEEKDAYS, clock_for, into_window
from apps.flows.engine.context import NodeContext
from apps.flows.engine.nodes.base import Node
from apps.flows.engine.registry import register_node
from apps.flows.engine.results import Fail, Schedule, StepResult

__all__ = ["WEEKDAYS", "SmartDelayNode"]

logger = logging.getLogger(__name__)

_UNITS = ("minutes", "hours", "days")


@register_node
class SmartDelayNode(Node):
    """Schedule a resume, honouring the node's sending window."""

    type = "smart_delay"
    # SPEC §7.1's inline-safe list does not include it. A delay is by definition
    # not the first reply a webhook is racing to produce, so there is nothing to
    # gain from running it inside the request.
    synchronous_safe = False

    def execute(self, ctx: NodeContext) -> StepResult:
        mode = ctx.config.get("mode")
        if mode == "duration":
            run_at = _from_duration(ctx)
        elif mode == "date":
            run_at = _from_date(ctx)
        else:
            return Fail(f"smart_delay node {ctx.node_id} has no mode")

        if run_at is None:
            return Fail(f"smart_delay node {ctx.node_id}: nothing to compute a delay from")

        adjusted = into_window(run_at, ctx.config.get("continue_window"), _clock(ctx))
        logger.debug("Execution %s: node %s sleeping until %s", ctx.execution.pk, ctx.node_id, adjusted)
        return Schedule(adjusted, resume_handle="default", config={"node_id": ctx.node_id})


# ---------------------------------------------------------------------------
# When
# ---------------------------------------------------------------------------


def _from_duration(ctx: NodeContext) -> datetime | None:
    duration = ctx.config.get("duration")
    if not isinstance(duration, dict):
        return None
    value, unit = duration.get("value"), duration.get("unit")
    if not isinstance(value, int) or value <= 0 or unit not in _UNITS:
        return None
    return timezone.now() + timedelta(**{str(unit): value})


def _from_date(ctx: NodeContext) -> datetime | None:
    """SPEC §11.5's "date {field or fixed datetime}"."""
    spec = ctx.config.get("date")
    if not isinstance(spec, dict):
        return None

    fixed = spec.get("datetime")
    if isinstance(fixed, str) and fixed:
        return _parse_instant(fixed, _clock(ctx))

    field = spec.get("field")
    if isinstance(field, str) and field:
        return _from_contact_field(ctx, field)
    return None


def _from_contact_field(ctx: NodeContext, name: str) -> datetime | None:
    """Read a date or datetime off the contact, by field name.

    Names rather than ids, matching the renderer's namespace (SPEC §9.2's
    "custom fields by name") and the action node's ``set_field`` — a smart delay
    reading ``{{renewal_date}}`` and a message rendering it should be naming the
    same thing the same way.
    """
    from apps.contacts.models import CustomField
    from apps.contacts.services import field_values_for

    contact = ctx.contact
    key = name.strip().casefold()

    system = getattr(contact, key, None) if key in ("created_at", "last_interaction_at") else None
    if isinstance(system, datetime):
        return system

    field = CustomField.objects.for_workspace(ctx.workspace_id).filter(name__iexact=key).first()
    if field is None:
        logger.warning("Execution %s: smart_delay names field %r, which does not exist.", ctx.execution.pk, name)
        return None

    value = field_values_for(contact).get(field.pk)
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        # A bare date means the start of that day, in whichever clock the node
        # is using — "on the 3rd" is a local statement, not a UTC one.
        return datetime.combine(value, time.min, tzinfo=_clock(ctx))
    logger.warning("Execution %s: field %r holds no date to wait for.", ctx.execution.pk, name)
    return None


def _parse_instant(raw: str, clock: tzinfo) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        logger.warning("smart_delay: %r is not an ISO-8601 instant.", raw)
        return None
    return parsed if timezone.is_aware(parsed) else parsed.replace(tzinfo=clock)


# ---------------------------------------------------------------------------
# Which clock
# ---------------------------------------------------------------------------


def _clock(ctx: NodeContext) -> tzinfo:
    """The timezone this node's window is expressed in."""
    window = ctx.config.get("continue_window")
    use_contact = bool(isinstance(window, dict) and window.get("use_contact_timezone"))
    return clock_for(ctx.contact, ctx.workspace, use_contact_timezone=use_contact)
