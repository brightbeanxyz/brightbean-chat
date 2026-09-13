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

**Gate and mark are separate, and the order matters.**
:func:`allows_reaching` decides, before anything is sent, whether a contact may
be reached. :func:`mark_reached` records that one was, and runs only once the
send has actually been accepted by the platform. Marking at the gate was simpler
and wrong: a provider rejection would then spend one of twenty-five monthly
slots on a message that never left the building, and a free organization with a
misconfigured channel could burn its whole allowance on failures.

The cost of that split is a bounded over-admission: two concurrent sends to two
new contacts can both pass the gate at 24 and both mark, leaving 26. That is
deliberate and it is the cheaper error. Closing it needs a lock held across the
platform call, which would serialise an entire broadcast fanout onto one
organization row, and the money at stake is zero — the plan it bounds is free.
The unique constraint still guarantees no *person* is counted twice, which is
the property the limit is actually about.

**Inbound is metered and never gated.** You cannot refuse a message somebody has
already sent, and ManyChat's own definition counts both directions. The
consequence is worth stating rather than discovering: inbound traffic a free
organization does not control can push it to its limit, and the refusal then
lands on the one thing it does control, which is starting new conversations.

**Nothing here may raise into the send path.** ``apps.messaging.services``
promises, twice in its own docstrings, that ``send_outbound`` never raises — the
flow engine follows a ``default`` edge from a ``FAILED`` row rather than
catching anything. So :func:`mark_reached` contains its database work in a
savepoint and turns a failure into a logged, bounded under-count. That is a
reversal of an earlier decision in this module, and the reasoning is worth
keeping: losing a mark gives one contact away free, while breaking contract 1
takes down every caller written against it.
"""

import logging
from typing import Any

from django.db import DatabaseError, transaction
from django.db import connection as db_connection
from django.utils import timezone

from apps.billing.entitlements import billing_enabled, limits_for
from apps.billing.models import ActiveContactMonth
from apps.common.uuid7 import uuid7

logger = logging.getLogger(__name__)

_TABLE = ActiveContactMonth._meta.db_table

#: Insert unless this contact is already marked for this period. The conflict
#: target is the columns of ``activecontactmonth_unique_period``.
_MARK_SQL = (
    # noqa is on this line because it is the expression ruff flags: the table
    # name is a module constant read off the model, and every value is bound.
    f"INSERT INTO {_TABLE} (id, workspace_id, organization_id, contact_id, period, created_at, updated_at) "  # noqa: S608
    f"VALUES (%s, %s, %s, %s, %s, %s, %s) "
    f"ON CONFLICT (contact_id, period) DO NOTHING"
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


def allows_reaching(organization: Any, contact: Any, *, limit: int | None, period: str) -> bool:
    """Whether this contact may be reached now. Reads only; writes nothing.

    A contact already counted this month passes, always. Only a new one is
    refused, so an in-flight conversation is never cut off half-way through —
    which is both the humane behaviour and the one that keeps a refusal
    explainable ("you have talked to 25 people this month").

    ``limit`` and ``period`` are arguments rather than being resolved here so a
    caller that already has them does not pay for them twice; ``limits_for``
    reaches a database query whenever billing is configured.
    """
    if limit is None:
        return True
    if is_contact_active(contact, period=period):
        return True
    return active_contact_count(organization, period=period) < limit


def mark_reached(organization: Any, contact: Any, *, period: str | None = None) -> bool:
    """Record that this contact was reached. Returns whether it was newly marked.

    **Never raises**, per the module docstring: the statement runs in its own
    savepoint so a database failure rolls back the mark alone and leaves the
    caller's transaction usable. A lost mark is a logged under-count of at most
    one contact; an exception here would break ``send_outbound``'s contract.

    Both ``workspace_id`` and ``organization_id`` are written explicitly. The
    model's ``save()`` derives them too, but this statement never reaches it, and
    a derivation only one of the two paths performs is a column that is correct
    in tests and null in production.
    """
    if not billing_enabled():
        return False

    # The guard covers the plan read as well as the insert. Reading the plan is
    # itself a query, and an earlier version left it outside — so a database in
    # trouble still broke the send path through the one line this function
    # exists to keep out of it.
    try:
        if limits_for(organization).active_contacts_per_month is None:
            # Unlimited: nothing to count, and no row is written. A test asserts
            # this table stays empty on a deployment with no billing.
            return False

        now = timezone.now()
        params = [
            uuid7(),
            contact.workspace_id,
            organization.pk,
            contact.pk,
            period or current_period(organization),
            now,
            now,
        ]
        # Its own savepoint, so a failure here cannot poison the surrounding
        # transaction — the send that is mid-flight has to be able to commit.
        with transaction.atomic(), db_connection.cursor() as cursor:
            cursor.execute(_MARK_SQL, params)
            return cursor.rowcount > 0
    except DatabaseError:
        logger.exception(
            "Could not record contact %s as active for organization %s; the month is under-counted by one",
            contact.pk,
            organization.pk,
        )
        return False


def meter(organization: Any, contact: Any) -> bool:
    """Mark a contact active for the current period. The inbound path's entry.

    Inbound is metered and never gated, so this is :func:`mark_reached` with no
    decision in front of it.
    """
    return mark_reached(organization, contact)


def reach(workspace: Any, contact: Any) -> bool:
    """Whether the plan permits reaching this contact. ``False`` means refuse.

    The single entry point ``apps.messaging`` gates on, so the send path carries
    one import and one branch rather than the plan's whole vocabulary. Writes
    nothing — :func:`mark_reached` is called after the send is accepted.

    **Never raises**, and costs nothing when billing is unconfigured: the
    settings read comes first, before the organization is resolved, so a
    self-hosted deployment pays no query at all.
    """
    if not billing_enabled():
        return True

    organization = None
    try:
        # Inside the guard from the first query onwards: resolving the
        # organization and reading the plan are both queries, and both were
        # outside it in an earlier version.
        organization = workspace.organization
        limit = limits_for(organization).active_contacts_per_month
        if limit is None:
            return True
        return allows_reaching(organization, contact, limit=limit, period=current_period(organization))
    except DatabaseError:
        # Contract 1 again: a billing read must not take a send down. Failing
        # open here gives at most one contact away, and only while the database
        # is already in trouble.
        logger.exception(
            "Could not read the contact allowance for organization %s; allowing the send",
            getattr(organization, "pk", "unknown"),
        )
        return True
