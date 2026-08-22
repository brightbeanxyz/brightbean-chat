from django.apps import AppConfig


class NotificationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.notifications"
    verbose_name = "Notifications"

    def ready(self) -> None:
        # Importing the registry is what populates it: the eight event types
        # this product ships are declared at module level in events.py, and a
        # later layer registering its own does the same from its own AppConfig.
        from apps.notifications import events  # noqa: F401 - import for side effects
        from apps.notifications.queue import register_handler_if_available

        # No-op until issue #5 (apps.queueing) merges; see queue.py.
        register_handler_if_available()
