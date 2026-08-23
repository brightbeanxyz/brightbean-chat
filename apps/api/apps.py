"""App configuration for the public REST API and outbound webhooks (issue #25)."""

from django.apps import AppConfig


class ApiConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.api"
    label = "api"
    verbose_name = "Public API"

    def ready(self) -> None:
        """Run the import side effects that have to happen once per process.

        Three registrations, all of them imports rather than calls so the
        registries stay the single place a registration is written down:

        * ``events`` connects one receiver to every event in contract 7's
          catalog, discovered from the installed apps rather than listed here.
        * ``delivery`` registers the ``webhook_delivery`` queue handler, the
          action type ``apps.queueing.models.ActionType`` reserved for this
          issue by name.
        * ``housekeeping`` puts the delivery-log prune into L2-C's hourly sweep.
        """
        from apps.api import delivery, housekeeping  # noqa: F401  (registration side effects)
        from apps.api.events import connect_catalog_receivers

        connect_catalog_receivers()
