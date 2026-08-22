"""App configuration for the messaging spine.

``ready()`` is where this app plugs into the three registries lower layers left
open for it. All three are import-or-call side effects performed once per
process, the convention every app here follows (see ``apps/channels/apps.py``):

* the **contract-6 dispatch seam** — persistence, then a no-op routing stage
  L4-A replaces in place;
* the **condition source** ``window``, declared with no implementation by
  ``apps.contacts.conditions`` and owned by this issue;
* the **queue handler** ``send_retry``, and this app's settings checks.

Imports live inside the method rather than at module scope so model imports stay
out of app loading.
"""

from django.apps import AppConfig


class MessagingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.messaging"
    verbose_name = "Messaging"

    def ready(self) -> None:
        from apps.messaging import (
            checks,  # noqa: F401  (registration side effect)
            handlers,  # noqa: F401  (registers send_retry)
        )
        from apps.messaging.conditions import register_window_source
        from apps.messaging.ingest import register_processors

        register_processors()
        register_window_source()
