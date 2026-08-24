from django.apps import AppConfig


class BroadcastsConfig(AppConfig):
    """SPEC §13's broadcasts, as their own app (issue #23).

    Its own Django app rather than half of ``apps.campaigns``: SPEC §5 says so,
    and ROADMAP's per-PR bar is what decided it — sequences (#22) and broadcasts
    (#23) are both Layer 6, and two workstreams sharing one app label cannot both
    ship a ``0001_initial``.
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.broadcasts"
    label = "broadcasts"
    verbose_name = "Broadcasts"

    def ready(self) -> None:
        """Registration is an import side effect, the house pattern.

        ``handlers`` claims the two queue action types, ``housekeeping`` the
        hourly settle sweep and ``notifications`` the in-app event copy. None of
        them is imported by anything else, so this is the only thing that puts
        them in their registries.
        """
        from apps.broadcasts import handlers, housekeeping, notifications  # noqa: F401
