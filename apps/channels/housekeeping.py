"""Retention for the raw webhook event log (SPEC §5).

    Prune rows older than 30 days via housekeeping job.

The window is not arbitrary and it is not only about disk. The unique constraint
on ``(connection, provider_event_id)`` is what makes a duplicate delivery a
no-op, so it is also the replay-protection window: an event whose row has been
pruned can be replayed, signature and all, and will be processed as new. Thirty
days is the specification's number, and the tradeoff is documented on
:class:`~apps.channels.models.WebhookEventLog`.

Registered with L2-C's housekeeping registry from ``ChannelsConfig.ready``, and
available as ``manage.py prune_webhook_events`` so a deployment without the
queue — or one running this issue before #5 merges — can still cron it.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from apps.channels.models import WebhookEventLog

logger = logging.getLogger(__name__)

__all__ = ["DEFAULT_RETENTION_DAYS", "prune_webhook_event_log"]

DEFAULT_RETENTION_DAYS = 30

#: How many rows one DELETE covers. Small enough that the lock is brief, large
#: enough that a backlog clears in a sane number of round trips.
PRUNE_BATCH_SIZE = 1000


def prune_webhook_event_log(older_than_days: int | None = None) -> int:
    """Delete event-log rows older than the retention window; return the count.

    Deletes in bounded batches rather than one statement. This table is written
    by every inbound webhook, so a first run on a deployment that has never
    pruned could otherwise hold a lock long enough to time out live deliveries.
    """
    if older_than_days is None:
        older_than_days = getattr(settings, "WEBHOOK_EVENT_LOG_RETENTION_DAYS", DEFAULT_RETENTION_DAYS)
    cutoff = timezone.now() - timedelta(days=older_than_days)

    total = 0
    while True:
        # Not a tenant model: WebhookEventLog hangs off the connection and has
        # no workspace column, so this is the plain manager rather than an
        # `.unscoped()` escape hatch.
        batch = list(
            WebhookEventLog.objects.filter(received_at__lt=cutoff).values_list("pk", flat=True)[:PRUNE_BATCH_SIZE]
        )
        if not batch:
            break
        deleted, _ = WebhookEventLog.objects.filter(pk__in=batch).delete()
        total += deleted
        if len(batch) < PRUNE_BATCH_SIZE:
            break

    if total:
        logger.info("Pruned %s webhook event log rows older than %s days", total, older_than_days)
    return total
