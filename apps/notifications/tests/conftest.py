"""Fixtures for the notification suite.

Since issue #5 merged, ``notify()`` hands email to the queue instead of sending
it inline, so a test that wants an outbox has to do the worker's job first.
"""

import pytest


@pytest.fixture
def drain_emails(db):
    """Run every queued ``notification_email`` action, as the worker would.

    Before #5 landed, ``enqueue_email`` always declined and the engine fell back
    to ``transaction.on_commit(send_delivery)`` — which is why so many of these
    tests were written around ``django_capture_on_commit_callbacks``. The queue
    path is the real one now, and the fallback only runs where ``apps.queueing``
    is absent, so tests that assert on rendering or on a delivery row drain
    first rather than capturing callbacks that no longer fire.
    """

    def drain() -> int:
        from apps.notifications.queue import HANDLER_TYPE, handle_notification_email
        from apps.queueing.models import ScheduledAction

        # unscoped(): the worker drains across tenants and so does this, and a
        # notification action may legitimately carry a NULL workspace.
        actions = list(ScheduledAction.objects.unscoped().filter(type=HANDLER_TYPE))
        failures: list[BaseException] = []
        for action in actions:
            # Each action is its own unit of work, exactly as it is for the
            # worker: one that raises must not stop the next from running, or a
            # test with two recipients would leave the second delivery sitting
            # at `queued` and assert on a state the queue would never reach.
            try:
                handle_notification_email(action.payload, action)
            except Exception as exc:  # noqa: BLE001 - re-raised below
                failures.append(exc)
        if failures:
            # Still surfaced, because "the handler raises so the queue retries"
            # is the contract a test needs to be able to see.
            raise failures[0]
        return len(actions)

    return drain
