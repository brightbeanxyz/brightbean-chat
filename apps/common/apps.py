from django.apps import AppConfig


class CommonConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.common"
    verbose_name = "Common"

    def ready(self) -> None:
        # Install the global log scrubber as early as any app code can run, so
        # it covers management commands, the worker and the test suite — not
        # just requests. Idempotent; safe under Django's double-import of
        # AppConfig.ready() in some autoreload paths.
        from apps.common.logging import install_scrubbing_record_factory

        install_scrubbing_record_factory()
