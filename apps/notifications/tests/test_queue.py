"""The seam to issue #5's task queue.

#5 is a parallel Layer-2 sibling that has not merged, so these tests exercise
both states: the one that exists today (no queue, send inline) and the one that
exists after #5 lands (enqueue, send in the worker).
"""

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
    sys.modules.update(added)
    with patch("apps.notifications.queue.django_apps.is_installed", return_value=True):
        yield types.SimpleNamespace(calls=calls, registered=registered)
    for name in added:
        sys.modules.pop(name, None)


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
