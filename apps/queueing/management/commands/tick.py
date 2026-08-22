"""A single drain pass, for cron-based hosts (SPEC §15).

    ``manage.py tick``: single drain pass (claim until empty or 55 s elapsed),
    for cron-based hosts.

Fifty-five seconds so a once-a-minute cron never overlaps itself — and if it
does anyway, that is fine: the claim statement makes overlapping ticks, and a
tick overlapping a running worker, safe by construction.
"""

import logging
from typing import Any

from django.core.management.base import BaseCommand

from apps.queueing.housekeeping import ensure_housekeeping_scheduled
from apps.queueing.worker import DEFAULT_BATCH_SIZE, drain, positive_int

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Drain due scheduled actions once and exit (for cron-based hosts)."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--batch-size", type=positive_int, default=DEFAULT_BATCH_SIZE)
        parser.add_argument("--max-seconds", type=float, default=55.0)

    def handle(self, *args: Any, **options: Any) -> None:
        ensure_housekeeping_scheduled()
        result = drain(batch_size=options["batch_size"], max_seconds=options["max_seconds"])
        logger.info(
            "Tick drained claimed=%s done=%s failed=%s retried=%s stranded=%s",
            result.claimed,
            result.done,
            result.failed,
            result.retried,
            result.stranded,
        )
        self.stdout.write(
            f"Processed {result.claimed} action(s): "
            f"{result.done} done, {result.retried} retrying, {result.failed} failed"
            f"{f', {result.stranded} stranded' if result.stranded else ''}."
        )
