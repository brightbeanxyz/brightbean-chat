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

        ``register_sms_hooks()`` is explicit rather than an import side effect,
        for the reason contract 6's registry gives: a registration that silently
        depends on an import having happened is what its duplicate guard exists
        to prevent. Order does not matter — ``apps.flows.triggers.hooks``
        dispatches by ``(priority, name)`` and imports nothing from this project,
        so it is safe to reach for from an app readied before ``apps.flows``.
        """
        from apps.channels import housekeeping  # noqa: F401  (registration side effect)
        from apps.channels.preview import register_processors as register_preview
        from apps.channels.providers import load_adapters
        from apps.channels.sms_compliance import register_sms_hooks

        load_adapters()
        # SPEC §16's flow preview, on contract 6's seam. It declares that it
        # runs late (apps.channels.ingest.LATE_ORDER) rather than relying on
        # this app's position in INSTALLED_APPS, which is before messaging.
        register_preview()
        # SPEC §6.6's STOP/HELP/START, at contract 6's hard_optout stage.
        register_sms_hooks()
        # The send_email runtime (#21). Contract 5's node registry is additive
        # and `apps/flows/engine/nodes/__init__.py` says L5-D/E register from
        # their own apps — so this import, not an entry in that package. L5-D
        # put `send_sms` in that package instead; both register correctly, and
        # reconciling the two is not this branch's to do.
        from apps.channels import nodes  # noqa: F401  (registration side effect)
