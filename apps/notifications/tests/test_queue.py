"""The seam to issue #5's task queue.

#5 is a parallel Layer-2 sibling that has not merged, so these tests exercise
both states: the one that exists today (no queue, send inline) and the one that
exists after #5 lands (enqueue, send in the worker).
"""

import smtplib
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core import mail

from apps.notifications import queue
from apps.notifications.engine import notify
from apps.notifications.models import DeliveryStatus, NotificationDelivery

LOOP_CAP_CONTEXT = {"flow_name": "Welcome", "contact_name": "Ada"}


@pytest.fixture
def fake_queueing():
    """A stand-in for apps.queueing, shaped like the contract in SPEC §15.

    Injected into ``sys.modules`` rather than built as a real Django app: the
    point is to prove the seam binds and calls correctly, not to reimplement a
    task queue. ``django_apps.is_installed`` is patched alongside, because the
    seam asks that question first.
    """
    calls: list[dict] = []
    registered: dict[str, object] = {}

    services = types.ModuleType("apps.queueing.services")
    services.enqueue = lambda **kwargs: calls.append(kwargs)  # type: ignore[attr-defined]

    handlers = types.ModuleType("apps.queueing.handlers")
    handlers.register_handler = lambda name, fn: registered.__setitem__(name, fn)  # type: ignore[attr-defined]

    package = types.ModuleType("apps.queueing")
    package.__path__ = []  # type: ignore[attr-defined]

    added = {
        "apps.queueing": package,
        "apps.queueing.services": services,
        "apps.queueing.handlers": handlers,
    }
    # Save whatever is there first. Since #5 landed, "apps.queueing" is a REAL
    # module, and popping it at teardown left the interpreter with a live
    # apps.queueing.worker whose parent package was gone — which broke an
    # unrelated queueing test that resolved a monkeypatch target by dotted
    # path. A stand-in has to be removed by restoring the original, not by
    # deleting the name.
    displaced = {name: sys.modules.get(name) for name in added}
    sys.modules.update(added)
    # The seam memoises what it bound, so a stand-in installed for one test
    # would otherwise still be bound in the next.
    queue.reset_bindings()
    with patch("apps.notifications.queue.django_apps.is_installed", return_value=True):
        yield types.SimpleNamespace(calls=calls, registered=registered)
    for name, previous in displaced.items():
        if previous is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous
    queue.reset_bindings()


@pytest.fixture(autouse=True)
def _clear_bindings():
    """No test may inherit another's resolved entry point."""
    queue.reset_bindings()
    yield
    queue.reset_bindings()


@pytest.mark.django_db
class TestWithoutTheQueue:
    """The state of `main` today."""

    def test_enqueue_declines_so_the_caller_sends_inline(self, tenancy):
        assert queue.queueing_available() is False

        delivery_count_before = NotificationDelivery.objects.count()
        assert queue.enqueue_email(types.SimpleNamespace(pk="x")) is False
        assert NotificationDelivery.objects.count() == delivery_count_before

    def test_registering_the_handler_is_a_harmless_no_op(self):
        assert queue.register_handler_if_available() is False

    def test_email_still_goes_out(self, tenancy, django_capture_on_commit_callbacks):
        with django_capture_on_commit_callbacks(execute=True):
            notify(tenancy.workspace, "flow_loop_cap_hit", context=LOOP_CAP_CONTEXT)

        assert len(mail.outbox) == 2
        assert set(NotificationDelivery.objects.values_list("status", flat=True)) == {DeliveryStatus.SENT}


@pytest.mark.django_db
class TestWithTheQueue:
    """What happens once #5 merges."""

    def test_the_handler_registers_under_the_agreed_type_name(self, fake_queueing):
        assert queue.register_handler_if_available() is True
        assert queue.HANDLER_TYPE == "notification_email"
        assert fake_queueing.registered["notification_email"] is queue.handle_notification_email

    def test_email_is_enqueued_instead_of_sent(self, tenancy, fake_queueing, django_capture_on_commit_callbacks):
        with django_capture_on_commit_callbacks(execute=True):
            notify(tenancy.workspace, "flow_loop_cap_hit", context=LOOP_CAP_CONTEXT)

        assert mail.outbox == []
        assert set(NotificationDelivery.objects.values_list("status", flat=True)) == {DeliveryStatus.QUEUED}
        assert len(fake_queueing.calls) == 2

    def test_the_payload_is_an_id_not_a_rendered_email(self, tenancy, fake_queueing):
        notify(tenancy.workspace, "flow_loop_cap_hit", context=LOOP_CAP_CONTEXT)

        payload = fake_queueing.calls[0]["payload"]
        assert set(payload) == {"delivery_id"}
        assert NotificationDelivery.objects.filter(pk=payload["delivery_id"]).exists()

    def test_the_idempotency_key_makes_a_double_enqueue_impossible(self, tenancy, fake_queueing):
        """SPEC §5 puts a UNIQUE on scheduled_action.idempotency_key, so the
        database refuses the second insert rather than sending twice."""
        notify(tenancy.workspace, "flow_loop_cap_hit", context=LOOP_CAP_CONTEXT)

        keys = [call["idempotency_key"] for call in fake_queueing.calls]
        assert len(keys) == len(set(keys))
        for call in fake_queueing.calls:
            assert call["idempotency_key"].endswith(call["payload"]["delivery_id"])

    def test_the_lookup_and_its_log_line_happen_once_per_process(self, tenancy, fake_queueing, caplog):
        """A 500-recipient fan-out must not write 500 identical INFO lines."""
        with caplog.at_level("INFO", logger="apps.notifications.queue"):
            notify(tenancy.workspace, "flow_loop_cap_hit", context=LOOP_CAP_CONTEXT)
            notify(tenancy.workspace, "flow_loop_cap_hit", context=LOOP_CAP_CONTEXT)

        assert len(fake_queueing.calls) == 4
        assert caplog.text.count("bound enqueue") == 1

    def test_the_workspace_travels_with_the_action(self, tenancy, fake_queueing):
        notify(tenancy.workspace, "flow_loop_cap_hit", context=LOOP_CAP_CONTEXT)

        assert fake_queueing.calls[0]["workspace_id"] == tenancy.workspace.pk

    def test_the_worker_handler_sends_the_email(self, tenancy, fake_queueing):
        notify(tenancy.workspace, "flow_loop_cap_hit", context=LOOP_CAP_CONTEXT)
        payload = fake_queueing.calls[0]["payload"]

        queue.handle_notification_email(payload)

        assert len(mail.outbox) == 1
        assert NotificationDelivery.objects.get(pk=payload["delivery_id"]).status == DeliveryStatus.SENT

    def test_a_deleted_delivery_is_a_no_op_not_a_failed_action(self, tenancy, fake_queueing, caplog):
        """The notification was deleted between enqueue and run. Failing the
        action would retry it five times to reach the same conclusion."""
        with caplog.at_level("INFO", logger="apps.notifications.queue"):
            queue.handle_notification_email({"delivery_id": "00000000-0000-0000-0000-000000000000"})

        assert mail.outbox == []
        assert "no longer exists" in caplog.text


@pytest.mark.django_db
class TestSignatureMismatch:
    def test_a_rejected_call_signature_falls_back_rather_than_losing_the_email(
        self, tenancy, fake_queueing, django_capture_on_commit_callbacks, caplog
    ):
        """If #5 lands with different keywords, the email must still go out —
        losing the queueing is recoverable, losing the email is not."""
        sys.modules["apps.queueing.services"].enqueue = lambda job: None

        with (
            caplog.at_level("ERROR", logger="apps.notifications.queue"),
            django_capture_on_commit_callbacks(execute=True),
        ):
            notify(tenancy.workspace, "flow_loop_cap_hit", context=LOOP_CAP_CONTEXT)

        assert len(mail.outbox) == 2
        assert "apps/notifications/queue.py" in caplog.text


class TestTheSeamIsActuallyOneFile:
    def test_only_queue_py_and_the_app_config_mention_queueing(self):
        """The claim in queue.py's docstring — "this one file is the repair" —
        is a gate, not a promise. If a later edit reaches for apps.queueing from
        the engine or a view, this fails and the repair stops being one file.
        """
        app_dir = Path(__file__).resolve().parents[1]
        allowed = {"queue.py", "apps.py"}

        offenders = sorted(
            path.name for path in app_dir.glob("*.py") if path.name not in allowed and "queueing" in path.read_text()
        )

        assert not offenders, f"these reach past the seam to apps.queueing: {offenders}"


@pytest.mark.django_db
class TestTheWorkerHandlerTellsTheQueueTheTruth:
    """The queue's only signal is whether the handler raised. A handler that
    swallows a transport error reports success, and SPEC §15's backoff never
    runs — so a transient SMTP outage becomes permanent."""

    def test_a_failed_send_raises_so_the_queue_retries(self, tenancy, fake_queueing):
        notify(tenancy.workspace, "flow_loop_cap_hit", users=[tenancy.owner], context=LOOP_CAP_CONTEXT)
        payload = fake_queueing.calls[0]["payload"]

        with (
            patch("django.core.mail.EmailMultiAlternatives.send", side_effect=smtplib.SMTPException("greylisted")),
            pytest.raises(queue.NotificationEmailError, match="failed"),
        ):
            queue.handle_notification_email(payload)

        assert NotificationDelivery.objects.get(pk=payload["delivery_id"]).status == DeliveryStatus.FAILED

    def test_a_successful_send_returns_quietly(self, tenancy, fake_queueing):
        notify(tenancy.workspace, "flow_loop_cap_hit", users=[tenancy.owner], context=LOOP_CAP_CONTEXT)
        payload = fake_queueing.calls[0]["payload"]

        assert queue.handle_notification_email(payload) is None
        assert len(mail.outbox) == 1

    def test_an_already_sent_delivery_is_not_mailed_twice(self, tenancy, fake_queueing):
        """The idempotency key stops a second action *row*; it does nothing
        about one row running twice, which is what zombie recovery does after a
        worker sends the mail and dies before marking the action done."""
        notify(tenancy.workspace, "flow_loop_cap_hit", users=[tenancy.owner], context=LOOP_CAP_CONTEXT)
        payload = fake_queueing.calls[0]["payload"]
        queue.handle_notification_email(payload)
        assert len(mail.outbox) == 1

        queue.handle_notification_email(payload)

        assert len(mail.outbox) == 1

    def test_a_retry_after_a_failure_is_still_attempted(self, tenancy, fake_queueing):
        """Only SENT is skipped — a FAILED row is exactly what a retry is for."""
        notify(tenancy.workspace, "flow_loop_cap_hit", users=[tenancy.owner], context=LOOP_CAP_CONTEXT)
        payload = fake_queueing.calls[0]["payload"]
        with (
            patch("django.core.mail.EmailMultiAlternatives.send", side_effect=smtplib.SMTPException("boom")),
            pytest.raises(queue.NotificationEmailError),
        ):
            queue.handle_notification_email(payload)

        queue.handle_notification_email(payload)

        assert len(mail.outbox) == 1
        assert NotificationDelivery.objects.get(pk=payload["delivery_id"]).status == DeliveryStatus.SENT
