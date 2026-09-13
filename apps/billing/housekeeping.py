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
