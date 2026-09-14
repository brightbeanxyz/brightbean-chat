"""App configuration for the optional Stripe billing integration.

**This app ships in every install and runs in almost none of them.** BrightBean
Chat is AGPL and self-hostable, and SPEC §1.1 promises a self-hoster one tier
with every feature available. That promise is kept by configuration, not by
packaging: with no Stripe keys set, :func:`apps.billing.entitlements.is_paid`
answers ``True`` for every organization, the checkout, portal and webhook routes
404, and nothing in this app ever runs. It is the ``SENTRY_DSN`` arrangement in
``config/settings/base.py`` — the code is present and inert.

Deliberately **not** mounted through ``config.urls._if_installed``. That helper
exists so an app can be dropped, and dropping this one would turn
``entitlements.is_paid`` — which callers all over the tree ask — into an
``ImportError``, forcing a late-resolving seam for a function whose whole job in
the off state is to return ``True``. It would also make "billing is off" a
different database schema rather than a different configuration, so an operator
who later wanted Stripe would need an ``INSTALLED_APPS`` edit and a migration
instead of two environment variables. Two tables that stay empty forever cost
less than either.
"""

from django.apps import AppConfig


class BillingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.billing"
    label = "billing"
    verbose_name = "Billing"

    def ready(self) -> None:
        """Import the modules whose registration is a side effect of import.

        ``checks`` registers a Django system check and ``housekeeping``
        registers two queue jobs; both do their work at import time, so this is
        the only thing that makes them exist. The import is inside ``ready``
        rather than at module scope because the app registry is not populated
        when this file is first read.
        """
        from apps.billing import checks, housekeeping  # noqa: F401 - imported for registration
