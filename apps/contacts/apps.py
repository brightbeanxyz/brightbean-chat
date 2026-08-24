from django.apps import AppConfig


class ContactsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.contacts"
    verbose_name = "Contacts"

    def ready(self) -> None:
        # All imported for a registration side effect, as apps/common/apps.py
        # does: system checks, the CSV import's queue handler
        # (apps.queueing.registry) and its file-retention sweep
        # (apps.queueing.housekeeping), and issue #29's queued erasure handler.
        # Registration in ready() rather than at module import is what those two
        # registries document.
        from apps.contacts import checks, erasure, housekeeping, imports  # noqa: F401
