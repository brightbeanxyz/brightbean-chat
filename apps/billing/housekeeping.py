"""Retention for the Stripe event log.

Registered with the hourly sweep by the decorator below, which runs when
``BillingConfig.ready`` imports this module — the shape
``apps.queueing.housekeeping`` documents and ``apps.channels.housekeeping``
follows.

The window bounds the table, not the replay risk. Stripe signs a timestamp into
every delivery and ``STRIPE_WEBHOOK_TOLERANCE_SECONDS`` rejects anything older
than five minutes, so a captured request is unusable long before its row is
pruned — unlike the platform webhooks, where the unique constraint *is* the
whole replay window.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from apps.billing.models import StripeEventLog
from apps.queueing.housekeeping import register_housekeeping_job

logger = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS = 30

__all__ = ["DEFAULT_RETENTION_DAYS", "prune_stripe_event_log"]


def prune_stripe_event_log() -> int:
    """Delete event rows older than the retention window. Returns the count."""
    days = getattr(settings, "STRIPE_EVENT_LOG_RETENTION_DAYS", DEFAULT_RETENTION_DAYS)
    cutoff = timezone.now() - timedelta(days=days)
    deleted, _ = StripeEventLog.objects.filter(received_at__lt=cutoff).delete()
    if deleted:
        logger.info("Pruned %s Stripe webhook event rows older than %s days", deleted, days)
    return deleted


@register_housekeeping_job("prune_stripe_event_log")
def _prune_stripe_event_log_job() -> str | None:
    """The zero-argument, string-returning shape the hourly sweep expects.

    A thin wrapper rather than decorating the function above, which returns a
    count the tests want and the sweep does not. Returning None on an empty
    prune keeps the hourly log quiet when there was nothing to do — the shape
    ``apps.channels.housekeeping`` established.
    """
    deleted = prune_stripe_event_log()
    return f"pruned {deleted} Stripe event rows" if deleted else None


#: How long to let a checkout sit unconfirmed before going and asking Stripe.
#: Long enough that the webhook has genuinely had its chance — Stripe usually
#: delivers in seconds — and short enough that somebody who paid is not left on
#: a free plan for an hour.
PENDING_CHECKOUT_MINUTES = 15


def reconcile_pending_checkouts() -> int:
    """Ask Stripe about checkouts whose webhook never arrived. Returns the count.

    **This is the part that makes a lost event survivable**, and it is worth more
    than any amount of care about event ordering: the ordering rules in
    ``apps.billing.events`` can only reason about events that arrived. A delivery
    that was dropped, or that failed while our database was down, leaves a
    customer who paid sitting on the free plan with nothing to notice it.

    Deliberately not triggered by the billing page. A synchronous fetch there
    would put a third-party HTTP call on a GET any logged-in user can repeat by
    reloading, and it would still not help the person who paid and never came
    back to look.
    """
    from apps.billing import events
    from apps.billing.entitlements import billing_enabled
    from apps.billing.models import BillingCustomer
    from apps.billing.stripe_client import StripeUnavailableError, retrieve_customer_with_subscriptions

    if not billing_enabled():
        return 0

    cutoff = timezone.now() - timedelta(minutes=PENDING_CHECKOUT_MINUTES)
    pending = BillingCustomer.objects.filter(
        checkout_pending_since__isnull=False,
        checkout_pending_since__lt=cutoff,
    ).exclude(stripe_customer_id="")

    reconciled = 0
    for row in pending:
        try:
            customer = retrieve_customer_with_subscriptions(row.stripe_customer_id)
        except StripeUnavailableError:
            # Leave the flag set so the next sweep tries again. Clearing it would
            # turn a transient Stripe outage into a permanently unreconciled row.
            logger.warning("Could not reconcile Stripe customer %s; will retry", row.stripe_customer_id)
            continue
        events.apply_snapshot(row, _first_subscription(customer))
        reconciled += 1

    if reconciled:
        logger.info("Reconciled %s pending Stripe checkouts", reconciled)
    return reconciled


def _first_subscription(customer: object) -> dict | None:
    """The customer's live subscription, or None if it has none."""
    subscriptions = getattr(customer, "subscriptions", None)
    data = getattr(subscriptions, "data", None) if subscriptions is not None else None
    if not data:
        return None
    first = data[0]
    if isinstance(first, dict):
        return first
    # The SDK hands back a StripeObject, which deliberately refuses dict() and
    # says so in the TypeError. `apps.billing.events` works in plain dicts —
    # that is what the webhook path gives it and what goes into the event log —
    # so this is the one place the two representations meet.
    to_dict = getattr(first, "to_dict", None)
    return to_dict() if callable(to_dict) else None


@register_housekeeping_job("reconcile_pending_checkouts")
def _reconcile_pending_checkouts_job() -> str | None:
    reconciled = reconcile_pending_checkouts()
    return f"reconciled {reconciled} pending checkouts" if reconciled else None
