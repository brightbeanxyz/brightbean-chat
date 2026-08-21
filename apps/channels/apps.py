"""App configuration for the channels framework."""

import logging

from django.apps import AppConfig

logger = logging.getLogger(__name__)


class ChannelsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.channels"
    verbose_name = "Channels"

    def ready(self) -> None:
        """Register adapters and the event-log prune job.

        Both are import-time side effects that have to happen exactly once per
        process, which is what ``ready()`` is for.
        """
        from apps.channels.providers import load_adapters

        load_adapters()
        self._register_housekeeping()

    @staticmethod
    def _register_housekeeping() -> None:
        """Hand the event-log prune to L2-C's housekeeping registry.

        ``apps.queueing`` (#5) is a **parallel sibling** of this issue, so the
        import may simply not exist yet. That is expected and is not an error —
        but it is logged rather than swallowed, because a silent ``except
        ImportError: pass`` is indistinguishable from a wiring bug once #5 has
        merged, and the difference is a table that grows forever.

        ``manage.py prune_webhook_events`` covers the gap either way.
        """
        from apps.channels.housekeeping import prune_webhook_event_log

        try:
            from apps.queueing.housekeeping import register_housekeeping_job
        except ImportError:
            logger.info(
                "apps.queueing is not installed, so the webhook event-log prune is not scheduled. "
                "Run `manage.py prune_webhook_events` from cron, or merge issue #5."
            )
            return
        register_housekeeping_job("channels.prune_webhook_event_log", prune_webhook_event_log)
