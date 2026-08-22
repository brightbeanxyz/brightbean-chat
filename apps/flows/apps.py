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

        The fifth registration is somebody else's module on purpose. SPEC §16's
        flow preview (issue #12) is a contract-6 processor that has to run
        **after** persistence and after L4-A's routing tail, and processors run
        in registration order, which is ``INSTALLED_APPS`` order —
        ``apps.channels`` is listed before ``apps.messaging``, so registering it
        from the channels app would put it first. This app is listed after
        messaging, which makes it the right place to say "and then the preview".
        ``apps.channels.preview``'s module docstring carries the full reasoning,
        and a test pins the resulting order.
        """
        from apps.channels.preview import register_processors as register_preview
        from apps.flows import handlers, housekeeping, notifications  # noqa: F401
        from apps.flows.engine import nodes  # noqa: F401

        register_preview()
