from django.apps import AppConfig


class QueueingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.queueing"
    verbose_name = "Task queue"

    def ready(self) -> None:
        # Registers this app's own handler and housekeeping job by import side
        # effect — the same convention every later layer follows (see the
        # module docstrings on registry.py and housekeeping.py). Importing here
        # rather than at module scope keeps model imports out of app loading.
        from apps.queueing import housekeeping  # noqa: F401
