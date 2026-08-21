"""The long-lived worker (SPEC §15). ``Procfile``'s ``worker:`` process.

Loop shape, per SPEC §15: "every 1 s, claim up to 50 due rows ... process each".
With one refinement: the sleep happens only when a batch came back *short*. A
full batch means there is more work waiting, and pausing a second between full
batches would cap throughput at 50 actions/second no matter how many workers
were running — an artificial ceiling on exactly the backlog the worker exists to
clear.

Run as many of these as you like. Safety comes from the claim statement
(``FOR UPDATE SKIP LOCKED``) and the contact advisory locks, not from there
being one of them.
"""

import logging
import signal
import time
from types import FrameType
from typing import Any

from django.core.management.base import BaseCommand
from django.db import connections

from apps.queueing.housekeeping import ensure_housekeeping_scheduled
from apps.queueing.worker import DEFAULT_BATCH_SIZE, BatchResult, run_batch

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Run the background task worker: claim due scheduled actions and process them."

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._stopping = False

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
        parser.add_argument("--interval", type=float, default=1.0, help="Seconds to idle when the queue is empty.")
        parser.add_argument("--once", action="store_true", help="Run a single batch and exit (tests, debugging).")
        parser.add_argument("--max-batches", type=int, default=0, help="Stop after N batches. 0 means never.")

    def handle(self, *args: Any, **options: Any) -> None:
        batch_size: int = options["batch_size"]
        interval: float = options["interval"]
        once: bool = options["once"]
        max_batches: int = options["max_batches"]

        self._install_signal_handlers()
        ensure_housekeeping_scheduled()

        logger.info("Worker started batch_size=%s interval=%s", batch_size, interval)
        totals = BatchResult()
        batches = 0

        while not self._stopping:
            self._refresh_connections()

            result = run_batch(batch_size)
            totals += result
            batches += 1

            if once or (max_batches and batches >= max_batches):
                break
            if result.claimed < batch_size:
                # Nothing more waiting: idle rather than spin on the database.
                self._sleep(interval)

        logger.info(
            "Worker stopped batches=%s claimed=%s done=%s failed=%s retried=%s",
            batches,
            totals.claimed,
            totals.done,
            totals.failed,
            totals.retried,
        )
        self.stdout.write(
            f"Processed {totals.claimed} action(s) in {batches} batch(es): "
            f"{totals.done} done, {totals.retried} retrying, {totals.failed} failed."
        )

    def _refresh_connections(self) -> None:
        """Drop connections the database may have closed under us.

        A worker can idle for hours between actions. Without this it keeps
        holding a socket that Postgres, or a pooler in front of it, hung up on
        long ago — and the failure surfaces as the *next* claim raising rather
        than as anything about connections. This is the same reaping Django
        does between web requests; with the default ``CONN_MAX_AGE=0`` it means
        a fresh connection per loop, which is why a busy deployment should set
        ``CONN_MAX_AGE`` rather than removing this.

        Skipped while a transaction is open. Closing a connection mid-transaction
        rolls it back, and the only way this loop runs inside one is a test
        harness that wrapped it — which is exactly the case that would otherwise
        lose the batch it just processed.
        """
        for connection in connections.all(initialized_only=True):
            if not connection.in_atomic_block:
                connection.close_if_unusable_or_obsolete()

    # -- shutdown -----------------------------------------------------------

    def _install_signal_handlers(self) -> None:
        """Finish the current batch on SIGTERM, then exit cleanly.

        A batch is already claimed and marked ``running`` in the database, so
        dying inside one is not data loss — zombie recovery would return the
        rows to ``pending`` after ten minutes. But ten minutes of delay on every
        deploy is a bad trade for the few hundred milliseconds it costs to
        finish the batch in hand, which is what every rolling deploy and
        ``docker stop`` sends this signal for.
        """
        for signum in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(signum, self._request_stop)
            except ValueError:
                # Not the main thread (a test harness, an embedded run). The
                # loop is still bounded by --once / --max-batches.
                logger.debug("Could not install handler for signal %s outside the main thread", signum)

    def _request_stop(self, signum: int, frame: FrameType | None) -> None:
        if self._stopping:
            return
        self._stopping = True
        logger.info("Received signal %s; finishing the current batch before exiting", signum)

    def _sleep(self, interval: float) -> None:
        """Sleep in short slices so a signal is noticed promptly."""
        deadline = time.monotonic() + interval
        while not self._stopping and time.monotonic() < deadline:
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
