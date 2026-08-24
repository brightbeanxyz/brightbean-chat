"""App configuration for sequences and rule triggers (issue #22, L6-A).

The label matters and is not a preference: ``apps/flows/picklists.py::_sequences``
resolves ``installed_model("campaigns", "apps.campaigns", "Sequence")``, so the
builder's sequence dropdown fills itself the moment this app is installed under
this exact label — and a different one would leave that resolver permanently
empty with nothing red to say so.

``ready()`` fills the four slots lower layers reserved for this issue. All of
them are additive registrations into somebody else's registry, none of them
edits the module that declares the slot:

* **action verbs** ``subscribe_sequence`` / ``unsubscribe_sequence``
  (ROADMAP contract 5) — schemas already ship in ``apps/flows/schema/nodes.py``;
* the **condition source** ``sequence`` (contract 8), declared with a ``None``
  handler by ``apps.contacts.conditions``;
* the queue handler for ``ActionType.SEQUENCE_STEP`` and the housekeeping sweep;
* the **rule-trigger binding**, which consumes the internal event catalog
  (contract 7) — *not* the inbound routing pipeline. Contract 6 is explicit
  about that, so there is deliberately no ``register_hook`` call anywhere in
  this app.

Imports live inside the method rather than at module scope so model imports stay
out of app loading, the convention every app here follows.
"""

from django.apps import AppConfig


class CampaignsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.campaigns"
    label = "campaigns"
    verbose_name = "Campaigns"

    def ready(self) -> None:
        from apps.campaigns import (
            handlers,  # noqa: F401  (registers sequence_step)
            housekeeping,  # noqa: F401  (registers the enrollment sweep)
            verbs,  # noqa: F401  (registers the two action verbs)
        )
        from apps.campaigns.conditions import register_sequence_source
        from apps.campaigns.rules import connect_rule_receivers

        register_sequence_source()
        connect_rule_receivers()
