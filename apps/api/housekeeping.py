"""Retention for the outbound-webhook delivery log (SPEC §17).

The settings page shows the last ``API_WEBHOOK_DELIVERY_LOG_KEEP`` deliveries
per endpoint, so that is what the table keeps. Unlike the inbound event log,
nothing depends on these rows for correctness — they are diagnostics — which is
why the rule is "newest N per webhook" rather than an age window: an endpoint
that fires twice a year should still show its last fifty, and one that fires
every second should not accumulate a million rows in a fortnight.

Registered with L2-C's hourly sweep by the decorator below, which runs when
``ApiConfig.ready`` imports this module.
"""

from __future__ import annotations

import logging

from django.conf import settings

from apps.queueing.housekeeping import register_housekeeping_job

logger = logging.getLogger(__name__)

__all__ = ["prune_webhook_deliveries"]

#: Rows per DELETE. The table is written by the worker, not by a request, so a
#: brief lock is cheap — but a first run on a long-lived deployment could still
#: cover a lot of rows.
PRUNE_BATCH_SIZE = 1000


def prune_webhook_deliveries(keep: int | None = None) -> int:
    """Trim every endpoint's delivery log to its newest ``keep`` rows.

    Cross-tenant by nature: housekeeping sweeps the deployment, and there is no
    workspace in scope to run it under. ``.unscoped()`` with this comment is the
    greppable form CONTRIBUTING.md asks for.
    """
    from apps.api.models import OutboundWebhook, WebhookDelivery

    if keep is None:
        keep = settings.API_WEBHOOK_DELIVERY_LOG_KEEP

    total = 0
    # Cross-tenant: the hourly sweep has no workspace, by design.
    webhook_ids = list(OutboundWebhook.objects.unscoped().values_list("pk", flat=True))
    for webhook_id in webhook_ids:
        while True:
            # Cross-tenant, same reason as above.
            doomed = list(
                WebhookDelivery.objects.unscoped()
                .filter(webhook_id=webhook_id)
                .order_by("-created_at", "-id")
                .values_list("pk", flat=True)[keep : keep + PRUNE_BATCH_SIZE]
            )
            if not doomed:
                break
            # Cross-tenant, same reason as above.
            deleted, _ = WebhookDelivery.objects.unscoped().filter(pk__in=doomed).delete()
            total += deleted
            if len(doomed) < PRUNE_BATCH_SIZE:
                break

    if total:
        logger.info("Pruned %s webhook delivery rows beyond the newest %s per endpoint", total, keep)
    return total


@register_housekeeping_job("prune_webhook_deliveries")
def _prune_webhook_deliveries_job() -> str | None:
    """The zero-argument, string-returning shape the hourly sweep expects."""
    deleted = prune_webhook_deliveries()
    return f"pruned {deleted} webhook delivery rows" if deleted else None
