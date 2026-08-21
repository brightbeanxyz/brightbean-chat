"""``manage.py prune_webhook_events`` — the housekeeping job, by hand.

The same function L2-C's registry schedules. It exists as a command because a
deployment must not depend on the task queue having merged to keep this table
bounded, and because an operator investigating disk usage wants to be able to
run it now.
"""

from typing import Any

from django.core.management.base import BaseCommand

from apps.channels.housekeeping import DEFAULT_RETENTION_DAYS, prune_webhook_event_log


class Command(BaseCommand):
    help = "Delete webhook event-log rows older than the retention window (SPEC §5)."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--older-than-days",
            type=int,
            default=None,
            help=(
                f"Retention window in days. Defaults to settings."
                f"WEBHOOK_EVENT_LOG_RETENTION_DAYS, or {DEFAULT_RETENTION_DAYS}."
            ),
        )

    def handle(self, *args: Any, **options: Any) -> None:
        deleted = prune_webhook_event_log(options["older_than_days"])
        self.stdout.write(self.style.SUCCESS(f"Pruned {deleted} webhook event log rows."))
