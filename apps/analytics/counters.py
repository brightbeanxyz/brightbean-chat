"""Incrementing ``node_stat_daily`` (SPEC §5, §18).

    Upserted counters, no per-event rows in v1 beyond ``message``.

**One statement, and that is the acceptance criterion.** "Parallel increments
never lose counts" rules out the obvious ORM shape — read the row, add one, save
— for the reason :mod:`apps.common.ratelimit` gives at length about
``DatabaseCache.incr``: two workers finishing a send at the same moment read the
same number and one write lands on top of the other. It also rules out
``get_or_create`` followed by ``update(F("sent") + 1)``, which is two round trips
with a unique-violation race in the middle.

``INSERT … ON CONFLICT (flow_id, node_id, date) DO UPDATE SET sent = <table>.sent
+ EXCLUDED.sent`` has neither problem: Postgres serialises the conflicting
inserters on the row itself, and the addition happens inside the statement, so
there is no window between a read and a write to lose anything in. The ORM cannot
express it — ``bulk_create(update_conflicts=True)`` *sets* the excluded value
rather than adding to it — so this is raw SQL, parameterised, in the shape
``apps/queueing/locks.py`` already uses for ``pg_advisory_xact_lock``. The
statement is a module constant with ``%s`` placeholders: nothing is interpolated
into it (SECURITY-BASELINE §7).

--------------------------------------------------------------------------
A counter must never cost a message
--------------------------------------------------------------------------

:func:`record_message_status` is called from inside the send pipeline. A failure
here — a migration not yet applied on one web worker, a deadlock — must not turn
a delivered message into a failed one, so the write takes its own savepoint and
every database error is logged and swallowed. Losing a count is a reporting bug;
losing a send is a product one.

--------------------------------------------------------------------------
Transitions, not statuses
--------------------------------------------------------------------------

:func:`deltas_for` takes the status a message moved *from* as well as the one it
moved *to*, and that is what makes every counter idempotent. A message walks
``queued → sent → delivered → read``; counting on arrival at each rung would
count ``delivered`` twice for a message that is later read, and re-counting a
status that was written twice would inflate ``sent``. Counting the *crossing* of
a rung cannot: crossing it twice is not a thing that happens.
"""

import logging
from datetime import date as date_type
from typing import Any

from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from apps.analytics.models import COUNTER_FIELDS
from apps.common.uuid7 import uuid7
from apps.messaging.models import DELIVERY_PROGRESS, MessageStatus

logger = logging.getLogger(__name__)

__all__ = ["bump", "deltas_for", "record_click", "record_message_status"]

_TABLE = "analytics_node_stat_daily"

#: Built once from :data:`apps.analytics.models.COUNTER_FIELDS` so the statement
#: and the model cannot disagree about which columns exist. Every value is a
#: placeholder; the only interpolation is over our own column names.
_UPSERT_SQL = (
    f"INSERT INTO {_TABLE} "  # noqa: S608 - names are module constants; every value is bound
    f'(id, workspace_id, flow_id, node_id, "date", '
    f"{', '.join(COUNTER_FIELDS)}, created_at, updated_at) "
    f"VALUES (%s, %s, %s, %s, %s, {', '.join(['%s'] * len(COUNTER_FIELDS))}, %s, %s) "
    f'ON CONFLICT (flow_id, node_id, "date") DO UPDATE SET '
    + ", ".join(f"{field} = {_TABLE}.{field} + EXCLUDED.{field}" for field in COUNTER_FIELDS)
    + ", updated_at = EXCLUDED.updated_at"
)


def bump(*, workspace_id: Any, flow_id: Any, node_id: str, day: date_type | None = None, **deltas: Any) -> None:
    """Add ``deltas`` to one node's counters for one UTC day.

    ``day`` defaults to today in UTC — see :class:`apps.analytics.models.NodeStatDaily`
    for why the bucket is not the workspace's local date. Unknown keys in
    ``deltas`` are a programming error and raise; zero deltas are a no-op.
    """
    unknown = set(deltas) - set(COUNTER_FIELDS)
    if unknown:
        raise ValueError(f"{sorted(unknown)} are not counters; known: {', '.join(COUNTER_FIELDS)}.")
    values = [int(deltas.get(field, 0)) for field in COUNTER_FIELDS]
    if not any(values):
        return

    now = timezone.now()
    params = [uuid7(), workspace_id, flow_id, node_id[:64], day or now.date(), *values, now, now]
    try:
        # Its own savepoint: a failure here rolls back the counter alone and
        # leaves the send's transaction usable. Without it, catching the error
        # below would hand the caller a transaction that can no longer commit.
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(_UPSERT_SQL, params)
    except DatabaseError:
        logger.exception("Could not record analytics counters for flow %s node %s", flow_id, node_id)


def deltas_for(previous: str, current: str) -> dict[str, int]:
    """Which counters one status transition moves. See the module docstring.

    ``DELIVERY_PROGRESS`` is :mod:`apps.messaging.models`' ladder — queued 0,
    sent 1, delivered 2, read 3 — and ``failed`` and ``deleted`` are deliberately
    absent from it. Both read as rank 0 here, "never got anywhere", which is what
    makes SPEC §9.5's late delivery receipt for a message that had already been
    written off (``failed → delivered``) count the arrival it reports.
    """
    before = DELIVERY_PROGRESS.get(previous, 0)
    after = DELIVERY_PROGRESS.get(current, 0)
    deltas = {
        "sent": 1 if before < 1 <= after else 0,
        "delivered": 1 if before < 2 <= after else 0,
        # A message can only be written off once; re-writing ``failed`` over
        # ``failed`` is a no-op in the counter as well as in the row.
        "failed": 1 if current == MessageStatus.FAILED and previous != MessageStatus.FAILED else 0,
    }
    return {field: value for field, value in deltas.items() if value}


def record_message_status(message: Any, *, previous: str, current: str) -> None:
    """Count one message's status transition against the node that sent it.

    Called only where the write actually landed — a compare-and-set that lost its
    race changed nothing and must count nothing.
    """
    deltas = deltas_for(previous, current)
    if not deltas:
        return
    from apps.analytics import attribution

    node = attribution.node_for(message)
    if node is None:
        # Not a flow node's message, or a preview run. See that module.
        return
    # Spelled out rather than splatted: ``clicked`` is deliberately absent,
    # because no status transition can produce one — a click arrives on the
    # ``/c/`` route and nowhere else (:func:`record_click`).
    bump(
        workspace_id=message.workspace_id,
        flow_id=node.flow_id,
        node_id=node.node_id,
        sent=deltas.get("sent", 0),
        delivered=deltas.get("delivered", 0),
        failed=deltas.get("failed", 0),
    )


def record_click(*, workspace_id: Any, flow_id: Any, node_id: str) -> None:
    """One click on a wrapped link. The ``/c/`` route's whole side effect."""
    bump(workspace_id=workspace_id, flow_id=flow_id, node_id=node_id, clicked=1)
