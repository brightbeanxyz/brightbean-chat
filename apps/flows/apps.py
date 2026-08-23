from django.apps import AppConfig


class FlowsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.flows"
    verbose_name = "Flows"

    def ready(self) -> None:
        """Wire the engine up by importing the modules that register things.

        Registries are populated as import side effects, which is the pattern
        ``apps.queueing.registry``'s docstring establishes: the node and verb
        runtimes (ROADMAP contract 5), the queue handlers, the stale-execution
        housekeeping job, and this app's derived notification event. Importing
        here rather than at module scope is what keeps them after the app
        registry is populated, so model imports are legal.

        Issue #11 adds three more, and one call. The trigger matchers and the
        ``route_event`` queue handler are import side effects like the rest;
        ``register_builtin_hooks()`` and ``register_routing()`` are explicit,
        because a registration that silently depends on an import having
        happened is the thing contract 6's guard exists to prevent.

        ``register_routing()`` claims the ``"routing"`` processor name
        unconditionally. ``apps.messaging`` is listed before this app in
        ``INSTALLED_APPS``, so its ``register_processors()`` has already put a
        no-op there; registering under an existing name **replaces in place**, so
        the real router inherits the slot after persistence. Its own guard then
        keeps it from ever putting the no-op back.
        """
        from apps.flows import handlers, housekeeping, notifications  # noqa: F401
        from apps.flows.engine import nodes  # noqa: F401
        from apps.flows.triggers import handlers as trigger_handlers  # noqa: F401
        from apps.flows.triggers import matching  # noqa: F401
        from apps.flows.triggers.pipeline import register_routing
        from apps.flows.triggers.stages import register_builtin_hooks

        register_builtin_hooks()
        register_routing()
