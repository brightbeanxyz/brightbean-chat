from django.apps import AppConfig


class FlowsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.flows"
    verbose_name = "Flows"

    def ready(self) -> None:
        """Wire the engine up by importing the modules that register things.

        Four registries are populated as import side effects, which is the
        pattern ``apps.queueing.registry``'s docstring establishes: the node and
        verb runtimes (ROADMAP contract 5), the queue handlers, the
        stale-execution housekeeping job, and this app's derived notification
        event. Importing here rather than at module scope is what keeps them
        after the app registry is populated, so model imports are legal.
        """
        from apps.flows import handlers, housekeeping, notifications  # noqa: F401
        from apps.flows.engine import nodes  # noqa: F401
