from django.apps import AppConfig


class ContactsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.contacts"
    verbose_name = "Contacts"

    def ready(self) -> None:
        # Imported for the @register side effect, as apps/common/apps.py does.
        from apps.contacts import checks  # noqa: F401
