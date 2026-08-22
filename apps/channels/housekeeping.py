"""Retention for the raw webhook event log and expired preview links (SPEC §5).

    Prune rows older than 30 days via housekeeping job.

The window is not arbitrary and it is not only about disk. The unique constraint
on ``(connection, provider_event_id)`` is what makes a duplicate delivery a
no-op, so it is also the replay-protection window: an event whose row has been
pruned can be replayed, signature and all, and will be processed as new. Thirty
days is the specification's number, and the tradeoff is documented on
:class:`~apps.channels.models.WebhookEventLog`.

Registered with L2-C's hourly sweep by the decorator below, which runs when
``ChannelsConfig.ready`` imports this module — the shape
``apps.queueing.housekeeping`` documents. It is also available as
``manage.py prune_webhook_events``, so an operator can force a prune without
waiting for the hour.

The registered name is ``prune_webhook_event_log``, which is the name
``apps.queueing.housekeeping.OPTIONAL_JOB_PATHS`` reserves for this job. That
matters: registering under any other name would leave that entry unresolved and
the sweep would try to import a second copy of this job every hour. Its entry
points at ``apps.channels.ingest.prune_webhook_event_log``, which is not where
this function lives — but a name already in the registry is skipped before the
path is ever tried, so the explicit registration wins and the stale path is
inert. Worth tidying in that module; not worth reaching into it from here.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from apps.channels.models import WebhookEventLog
from apps.queueing.housekeeping import register_housekeeping_job

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


@register_housekeeping_job("prune_webhook_event_log")
def _prune_webhook_event_log_job() -> str | None:
    """The zero-argument, string-returning shape the hourly sweep expects.

    A thin wrapper rather than decorating :func:`prune_webhook_event_log`
    directly: that one takes a window and returns a count, which is what the
    management command and the tests want, and the sweep wants neither. Returning
    None on an empty prune keeps the hourly log quiet when there was nothing to
    do.
    """
    deleted = prune_webhook_event_log()
    return f"pruned {deleted} webhook event log rows" if deleted else None


@register_housekeeping_job("prune_flow_preview_links")
def _prune_flow_preview_links_job() -> str | None:
    """Clear out spent "test on Telegram" links (issue #12).

    A separate job rather than a second statement inside the one above, because
    the two have unrelated retention rules and an operator reading the hourly
    log should see which one did the work. Imported late: the preview module
    reaches into the flow engine, and this module is imported from
    ``ChannelsConfig.ready``.
    """
    from apps.channels.preview import prune_expired_links

    deleted = prune_expired_links()
    return f"pruned {deleted} expired flow preview links" if deleted else None
