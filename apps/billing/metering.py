"""The active-contact meter: who a workspace reached this month.

The limit being metered is ManyChat's, transcribed: *25 Active Contacts per
month*, where they define an active contact as a person you interacted with
during the period, once, however many messages passed — *"A contact counts as
'active' when messages are sent **or received**."*

**It is a set, not a counter.** ``apps/media_library/quotas.py`` makes the
argument this module takes as its premise: a counter is a second source of truth
that drifts the first time a delete path forgets to decrement it. A set has no
decrement. Membership is asserted with an idempotent
``INSERT … ON CONFLICT DO NOTHING``; the count is a ``COUNT(*)``, derived rather
than stored; and deleting a contact cascades its row away, which is not drift but
exactly the "remove extras" half of the plan's semantics.

**Inbound is metered and never gated.** You cannot refuse a message somebody has
already sent, and ManyChat's own definition counts both directions. The
consequence is worth stating rather than discovering: inbound traffic a free
organization does not control can push it to its limit, and the refusal then
lands on the one thing it does control, which is sending.

**This module must not fail open, and that is the opposite of its neighbour.**
``apps.analytics.counters`` swallows every ``DatabaseError`` under a savepoint
because "a counter must never cost a message" — losing a count there is a
reporting bug. Losing a mark here means a free organization's twenty-sixth
contact is free forever. So the mark rides inside the caller's transaction: if it
fails, the send decision rolls back with it and the message stays queued for a
retry that will mark it properly. There is deliberately no
``except DatabaseError: pass`` anywhere in this file, and adding one would be a
correctness regression rather than the hardening it looks like.
"""

import logging
from typing import Any

from django.db import connection, transaction
from django.utils import timezone

from apps.billing.entitlements import PlanLimitError, limits_for
from apps.billing.models import ActiveContactMonth

logger = logging.getLogger(__name__)

_TABLE = ActiveContactMonth._meta.db_table

#: Insert if this contact is not already marked for this period, and report
#: whether it inserted. ``RETURNING id`` is what makes the row lock in
#: :func:`mark_contact_active` affordable — see the comment there.
_MARK_SQL = (
    # noqa is on this line because it is the expression ruff flags: the table
    # name is a module constant read off the model, and every value is bound.
    f"INSERT INTO {_TABLE} (id, workspace_id, organization_id, contact_id, period, created_at, updated_at) "  # noqa: S608
    f"VALUES (%s, %s, %s, %s, %s, %s, %s) "
    f"ON CONFLICT (contact_id, period) DO NOTHING "
    f"RETURNING id"
)


def current_period(organization: Any) -> str:
    """The calendar month, ``"YYYY-MM"``, in the organization's timezone.

    Deliberately **not** the Stripe billing period. A free organization has no
    subscription and therefore no period to borrow, and it is the only plan this
    meter is ever read for — so a Stripe-derived window would make the one plan
    with no Stripe object depend on one. It would also not survive
    cancel-and-resubscribe: a new subscription would silently reopen a month
    that had already been spent.
    """
    from zoneinfo import ZoneInfo

    name = getattr(organization, "default_timezone", "") or "UTC"
    try:
        tz = ZoneInfo(name)
    except Exception:  # noqa: BLE001 - a stored timezone can be anything; UTC is the safe read
        tz = ZoneInfo("UTC")
    return timezone.now().astimezone(tz).strftime("%Y-%m")


def active_contact_count(organization: Any, *, period: str) -> int:
    """How many distinct contacts this organization reached in ``period``.

    Reads across the organization's workspaces through the denormalised
    ``organization_id`` column rather than looping, because this runs on the
    send path. ``.unscoped()`` with the reason CONTRIBUTING.md asks for: the row
    is a billing fact about an *organization* that happens to name a contact,
    and a plan is bought by an organization, so a per-workspace figure would be
    the wrong number rather than a safer one.
    """
    return ActiveContactMonth.objects.unscoped().filter(organization=organization, period=period).count()


def is_contact_active(contact: Any, *, period: str) -> bool:
    """Whether this contact has already been counted for ``period``."""
    return ActiveContactMonth.objects.unscoped().filter(contact=contact, period=period).exists()


def check_can_reach_contact(organization: Any, contact: Any, *, period: str) -> None:
    """Refuse reaching a *new* contact once the month's allowance is spent.

    A contact already counted this month passes, always. Only a new one is
    refused, so an in-flight conversation is never cut off half-way through —
    which is both the humane behaviour and the one that keeps a refusal
    explainable ("you have talked to 25 people this month").
    """
    limit = limits_for(organization).active_contacts_per_month
    if limit is None:
        return
    if is_contact_active(contact, period=period):
        return
    if active_contact_count(organization, period=period) < limit:
        return
    raise PlanLimitError(
        f"Your plan includes {limit} contacts a month, and you have reached that many. "
        "Upgrade to keep starting new conversations.",
        code="plan_active_contacts",
    )


def mark_contact_active(contact: Any, *, period: str, organization_id: Any = None) -> bool:
    """Record that this contact was reached. Returns whether it was newly marked.

    Both ``workspace_id`` and ``organization_id`` are written explicitly. The
    model's ``save()`` derives them too, but this statement never reaches it, and
    a derivation only one of the two paths performs is a column that is correct
    in tests and null in production.

    **On concurrency.** Two simultaneous sends to two new contacts at 24 would
    both pass :func:`check_can_reach_contact` and land the organization at 26.
    The obvious fix — ``media_library``'s row lock — would serialise an entire
    broadcast fanout onto one organization row, which is far worse than the
    problem. What makes a lock affordable is that it is only needed when the
    insert actually *created* something, and ``RETURNING id`` says so: the caller
    re-checks under the lock on a real insert only, which for a free
    organization is at most twenty-five times a month and for everybody else is
    never.
    """
    from uuid6 import uuid7

    now = timezone.now()
    params = [
        uuid7(),
        contact.workspace_id,
        organization_id or contact.workspace.organization_id,
        contact.pk,
        period,
        now,
        now,
    ]
    # No savepoint and no swallowed DatabaseError: this rides in the caller's
    # transaction on purpose. See the module docstring.
    with connection.cursor() as cursor:
        cursor.execute(_MARK_SQL, params)
        return cursor.fetchone() is not None


def meter(organization: Any, contact: Any) -> bool:
    """Mark a contact active for the current period, rolling back an overshoot.

    The whole write path in one call, for the two sites that use it. Returns
    whether the contact was newly counted.

    The row lock is taken only when the insert created a row, which is the
    arrangement :func:`mark_contact_active` explains. Re-counting under it is
    what turns "two concurrent sends both saw 24" into a refusal for the second
    one.
    """
    period = current_period(organization)
    limit = limits_for(organization).active_contacts_per_month
    if limit is None:
        # Unlimited: no row is written at all. A self-hosted install is not
        # metered, and a test asserts this table stays empty there.
        return False

    with transaction.atomic():
        created = mark_contact_active(contact, period=period, organization_id=organization.pk)
        if not created:
            return False
        _lock_organization(organization.pk)
        if active_contact_count(organization, period=period) > limit:
            # Somebody else took the last slot between the check and the insert.
            # Undo this mark rather than admitting an extra contact.
            ActiveContactMonth.objects.unscoped().filter(contact=contact, period=period).delete()
            raise PlanLimitError(
                f"Your plan includes {limit} contacts a month, and you have reached that many.",
                code="plan_active_contacts",
            )
    return True


def _lock_organization(organization_id: Any) -> None:
    """Take the organization row lock, so the re-count above is serialised."""
    from apps.organizations.models import Organization

    Organization.objects.filter(pk=organization_id).select_for_update().only("id").first()
