"""App configuration for the analytics counters and stats views (issue #26).

SPEC §2 names ``apps/analytics/ counters and stats views`` and this is it. The
label matters for the same reason ``apps.campaigns``' does: other apps reach
this one through :func:`django.apps.apps.is_installed` rather than through a
module-level import, so a different label would leave those seams permanently
answering "not installed" with nothing red to say so.

There is deliberately **no** ``ready()``. Every other app here registers into a
lower layer's registry from one; this app is read *by* two lower layers instead
— ``apps.messaging.analytics`` and ``apps.flows.analytics`` resolve it late, the
way ``apps.flows.messaging`` resolves the messaging facade — so there is nothing
to register and nothing to import at boot.
"""

from django.apps import AppConfig


class AnalyticsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.analytics"
    label = "analytics"
    verbose_name = "Analytics"
