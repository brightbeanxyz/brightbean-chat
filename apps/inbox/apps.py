from django.apps import AppConfig


class InboxConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.inbox"
    label = "inbox"
    verbose_name = "Inbox"

    def ready(self) -> None:
        """Register the three seams issue #24 fills. All additive, none an edit.

        Contract 6's ``post_persist`` stage, two queue handlers, and one
        notification event. Every one of them is a registration into a registry a
        lower layer built and left open, so nothing in ``apps.flows``,
        ``apps.queueing`` or ``apps.notifications`` changes to accommodate this
        app — which is the property those registries exist to have.

        Imported here rather than at module scope so the app registry is already
        populated and the model imports inside are legal. The queue handlers and
        the notification event register as import side effects, the pattern
        ``apps.queueing.registry``'s docstring establishes;
        ``register_inbox_hooks()`` is an explicit call, following
        ``FlowsConfig.ready()``, because a registration that silently depends on
        an import having happened is exactly what goes missing in a refactor.
        """
        from apps.inbox import handlers, notifications  # noqa: F401
        from apps.inbox.routing import register_inbox_hooks

        register_inbox_hooks()
