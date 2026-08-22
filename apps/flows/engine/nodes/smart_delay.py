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

**A date-mode delay with nothing to compute from fails the run**, and that is
deliberate. "Wait until the renewal date" with an empty renewal date has two
possible readings, and the other one — treat it as *now* — sends the renewal
reminder immediately to everyone missing the field. A visible failure with an
in-app notification is the recoverable mistake; a burst of wrong messages is
not.
"""

import logging
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.utils import timezone

from apps.flows.engine.context import NodeContext
from apps.flows.engine.nodes.base import Node
from apps.flows.engine.registry import register_node
from apps.flows.engine.results import Fail, Schedule, StepResult

__all__ = ["SmartDelayNode"]

logger = logging.getLogger(__name__)

_UNITS = ("minutes", "hours", "days")

#: SPEC §11.5's day names, in ``date.weekday()`` order.
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

#: How far to look for the next allowed window. Eight days rather than seven so
#: a search starting mid-day still finds the same weekday a week later.
_WINDOW_SEARCH_DAYS = 8


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

        adjusted = _into_window(run_at, ctx.config.get("continue_window"), _clock(ctx))
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
    if isinstance(window, dict) and window.get("use_contact_timezone"):
        contact_zone = _zone(getattr(ctx.contact, "timezone", ""))
        if contact_zone is not None:
            return contact_zone
    workspace_zone = _zone(getattr(ctx.workspace, "effective_timezone", ""))
    return workspace_zone or timezone.get_current_timezone()


def _zone(name: Any) -> tzinfo | None:
    if not isinstance(name, str) or not name.strip():
        return None
    try:
        return ZoneInfo(name.strip())
    except (ZoneInfoNotFoundError, ValueError):
        # Contact timezones come from platform profiles, which are
        # attacker-controlled (SECURITY-BASELINE §2). A bad one falls back.
        logger.warning("smart_delay: %r is not a timezone; falling back.", name)
        return None


# ---------------------------------------------------------------------------
# The sending window
# ---------------------------------------------------------------------------


def _into_window(run_at: datetime, window: Any, clock: tzinfo) -> datetime:
    """Move ``run_at`` forward to the next moment the window allows.

    Forward only. A delay that finished outside the window has to wait for the
    window to open; one that finished inside it fires then and there.
    """
    if not isinstance(window, dict) or not window.get("enabled"):
        return run_at

    start, end = _time(window.get("from")), _time(window.get("to"))
    if start is None or end is None or start >= end:
        # An empty or inverted window has no "next allowed moment" to find, and
        # the builder lets an author save one (only `enabled` is required).
        # Ignoring it delivers late-ish; honouring it would deliver never.
        logger.warning("smart_delay: window %s–%s is not a usable range; ignoring it.", start, end)
        return run_at

    days = _days(window.get("days"))
    local = run_at.astimezone(clock)
    if _inside(local, days, start, end):
        return run_at

    for offset in range(_WINDOW_SEARCH_DAYS):
        day = (local + timedelta(days=offset)).date()
        if WEEKDAYS[day.weekday()] not in days:
            continue
        candidate = datetime.combine(day, start, tzinfo=clock)
        if candidate >= local:
            return candidate.astimezone(run_at.tzinfo)
    return run_at  # pragma: no cover - unreachable while `days` is non-empty


def _inside(local: datetime, days: frozenset[str], start: time, end: time) -> bool:
    return WEEKDAYS[local.date().weekday()] in days and start <= local.time() <= end


def _days(raw: Any) -> frozenset[str]:
    """The allowed weekdays. An empty list means every day.

    "Enabled, hours set, no days ticked" is two clicks away in the builder and
    reads as "these hours, any day" — not as "never", which is what an empty set
    would mean if taken literally.
    """
    if not isinstance(raw, list):
        return frozenset(WEEKDAYS)
    picked = frozenset(str(day).lower() for day in raw if str(day).lower() in WEEKDAYS)
    return picked or frozenset(WEEKDAYS)


def _time(raw: Any) -> time | None:
    if not isinstance(raw, str):
        return None
    try:
        return time.fromisoformat(raw)
    except ValueError:
        return None
