"""App configuration for the channels framework."""

from django.apps import AppConfig


class ChannelsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.channels"
    verbose_name = "Channels"

    def ready(self) -> None:
        """Run the import side effects that have to happen once per process.

        Adapters register themselves on import, and importing
        ``apps.channels.housekeeping`` is what puts the event-log prune into
        L2-C's hourly sweep — the shape ``apps.queueing.housekeeping``
        documents. Both are imports rather than calls, so the registries stay
        the single place a registration is written down.
        """
        from apps.channels import housekeeping  # noqa: F401  (registration side effect)
        from apps.channels.providers import load_adapters

        load_adapters()
