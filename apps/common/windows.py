"""Sending windows: moving an instant forward into the next allowed slot.

SPEC §11.5 writes the shape once — ``{enabled, days[], from, to,
use_contact_timezone}`` — and two features compute against it: the
``smart_delay`` node (:mod:`apps.flows.engine.nodes.smart_delay`) and a
sequence step's ``send_window`` (SPEC §12, issue #22). They are the same
arithmetic, so it lives here rather than twice.

The module is deliberately model-free. :func:`into_window` and :func:`zone` take
plain values, and :func:`clock_for` reads two attributes by name off whatever it
is handed — so this stays importable from any layer and testable without a
database.

**Forward only.** A moment that already falls inside the window is returned
unchanged; one outside it waits for the window to open. Nothing here ever moves
an instant backwards, which is what makes "delay at least N, then the window"
true rather than approximately true.

**Bad configuration delays, it does not cancel.** An empty or inverted range has
no "next allowed moment" to find, and the builder lets an author save one (only
``enabled`` is required). Ignoring it delivers late-ish; honouring it would
deliver never.
"""

import logging
from datetime import datetime, time, timedelta, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.utils import timezone

__all__ = ["WEEKDAYS", "clock_for", "into_window", "zone"]

logger = logging.getLogger(__name__)

#: SPEC §11.5's day names, in ``date.weekday()`` order.
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

#: How far to look for the next allowed window. Eight days rather than seven so
#: a search starting mid-day still finds the same weekday a week later.
_WINDOW_SEARCH_DAYS = 8


def zone(name: Any) -> tzinfo | None:
    """``ZoneInfo(name)``, or ``None`` for anything unusable.

    Contact timezones come from platform profiles, which are attacker-controlled
    (SECURITY-BASELINE §2). A bad one has to fall back rather than raise: it
    should delay a message, not end a run.
    """
    if not isinstance(name, str) or not name.strip():
        return None
    try:
        return ZoneInfo(name.strip())
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("%r is not a timezone; falling back.", name)
        return None


def clock_for(contact: Any, workspace: Any, *, use_contact_timezone: bool) -> tzinfo:
    """The timezone a window is expressed in.

    ``use_contact_timezone`` picks the contact's own zone when it has a usable
    one, and the workspace's ``effective_timezone`` (which already falls back to
    the organization's default) otherwise. Attributes are read with ``getattr``
    so a caller may pass anything carrying them — the node context does, and so
    does a bare model instance.
    """
    if use_contact_timezone:
        contact_zone = zone(getattr(contact, "timezone", ""))
        if contact_zone is not None:
            return contact_zone
    return zone(getattr(workspace, "effective_timezone", "")) or timezone.get_current_timezone()


def into_window(run_at: datetime, window: Any, clock: tzinfo) -> datetime:
    """Move ``run_at`` forward to the next moment ``window`` allows.

    ``window`` is SPEC §11.5's object or anything falsy; a window that is absent,
    malformed or not ``enabled`` returns ``run_at`` untouched.

    The search walks whole local days rather than doing timezone arithmetic on
    the instant, so a window that straddles a DST transition still opens at the
    wall-clock time the author configured: ``datetime.combine(day, start,
    tzinfo=clock)`` resolves the offset for *that* day.
    """
    if not isinstance(window, dict) or not window.get("enabled"):
        return run_at

    start, end = _time(window.get("from")), _time(window.get("to"))
    if start is None or end is None or start >= end:
        logger.warning("Window %s-%s is not a usable range; ignoring it.", start, end)
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

    "Enabled, hours set, no days ticked" is two clicks away in either editor and
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
