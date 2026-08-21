"""Shared helpers for the queueing tests.

The handler registry is process-global, so every test that registers one has to
put it back — otherwise a test that asserts "unknown type fails" passes or fails
depending on which test ran first.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Any

from django.utils import timezone

from apps.queueing import housekeeping, registry
from apps.queueing.models import ScheduledAction


@contextmanager
def temporary_handler(action_type: str, func: registry.Handler) -> Iterator[registry.Handler]:
    """Register a handler for the duration of a test, then restore the registry."""
    previous = registry.get_handler(action_type)
    registry.register_handler(action_type, replace=True)(func)
    try:
        yield func
    finally:
        if previous is None:
            registry._HANDLERS.pop(action_type, None)
        else:
            registry._HANDLERS[action_type] = previous


@contextmanager
def only_housekeeping_jobs(**jobs: housekeeping.HousekeepingJob) -> Iterator[None]:
    """Run the sweep with exactly these jobs and nothing else.

    Replaces the whole registry rather than adding to it, so a test asserting
    "one job failed" is not also running zombie recovery against whatever rows
    the test happens to have created.
    """
    previous = dict(housekeeping._JOBS)
    housekeeping._JOBS.clear()
    housekeeping._JOBS.update(jobs)
    try:
        yield
    finally:
        housekeeping._JOBS.clear()
        housekeeping._JOBS.update(previous)


def make_action(workspace: Any = None, **overrides: Any) -> ScheduledAction:
    """A due, pending action. Straight through the model so tests can set anything."""
    fields: dict[str, Any] = {
        "workspace": workspace,
        "run_at": timezone.now() - timedelta(seconds=1),
        "type": "test_action",
        "payload": {},
    }
    fields.update(overrides)
    return ScheduledAction.objects.create(**fields)
