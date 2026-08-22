"""The seam to issue #5's task queue.

Rewritten once #5 merged. These tests used to run against a hand-built stand-in
for ``apps.queueing`` — necessary while #5 was an unmerged sibling, and the
reason a whole layer shipped with the seam broken: the stand-in was built from
the same guess as the code, so both were wrong together and the suite stayed
green. Every guess was wrong. ``register_handler`` is a decorator factory, not
``register(type, fn)``; handlers take ``(payload, action)``; and the enqueue
entry point is ``schedule``, not ``enqueue``.

So the stand-in is gone. ``apps.queueing`` is in ``INSTALLED_APPS``, and these
tests call it — a ``ScheduledAction`` row is the assertion. The only thing still
simulated is the app being *absent*, which is a real deployment shape rather
than a guess about an API.
"""

import smtplib
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core import mail
from django.utils import timezone

from apps.notifications import queue
from apps.notifications.engine import notify
from apps.notifications.models import DeliveryStatus, NotificationDelivery
from apps.queueing.models import ScheduledAction
from apps.queueing.registry import get_handler

LOOP_CAP_CONTEXT = {"flow_name": "Welcome", "contact_name": "Ada"}


def queued_actions():
    """Every notification_email row. unscoped(): the worker drains all tenants."""
    return list(ScheduledAction.objects.unscoped().filter(type=queue.HANDLER_TYPE).order_by("created_at"))


@pytest.fixture
def without_queueing():
    """Make the seam believe ``apps.queueing`` is not installed.

    Patching ``is_installed`` rather than tearing modules out of ``sys.modules``:
    that is the question the seam actually asks, and the module surgery the old
    fixture did was what left a live ``apps.queueing.worker`` whose parent
    package had been deleted.
    """
    queue.reset_bindings()
    with patch("apps.notifications.queue.django_apps.is_installed", return_value=False):
        yield
    queue.reset_bindings()


@pytest.fixture(autouse=True)
def _fresh_bindings():
    queue.reset_bindings()
    yield
    queue.reset_bindings()


@pytest.mark.django_db
class TestWithoutTheQueue:
    """A deployment that does not install apps.queueing still sends email."""

    def test_enqueue_declines_so_the_caller_sends_inline(self, tenancy, without_queueing):
        import types

        assert queue.queueing_available() is False
        assert queue.enqueue_email(types.SimpleNamespace(pk="x")) is False

    def test_registering_the_handler_is_a_harmless_no_op(self, without_queueing):
        assert queue.register_handler_if_available() is False

    def test_email_still_goes_out(self, tenancy, without_queueing, django_capture_on_commit_callbacks):
        with django_capture_on_commit_callbacks(execute=True):
            notify(tenancy.workspace, "flow_loop_cap_hit", context=LOOP_CAP_CONTEXT)

        assert len(mail.outbox) == 2
        assert set(NotificationDelivery.objects.values_list("status", flat=True)) == {DeliveryStatus.SENT}
        assert queued_actions() == []


@pytest.mark.django_db
class TestWithTheQueue:
    def test_the_handler_registers_under_the_agreed_type_name(self):
        assert queue.register_handler_if_available() is True
        assert queue.HANDLER_TYPE == "notification_email"
        assert get_handler("notification_email") is queue.handle_notification_email

    def test_the_app_config_registered_it_at_boot(self):
        """The defect this file exists to prevent a repeat of: registration
        failed on every boot, the failure was caught and logged, and email
        silently sent inline for a whole layer."""
        assert get_handler(queue.HANDLER_TYPE) is not None

    def test_email_is_enqueued_instead_of_sent(self, tenancy, django_capture_on_commit_callbacks):
        with django_capture_on_commit_callbacks(execute=True):
            notify(tenancy.workspace, "flow_loop_cap_hit", context=LOOP_CAP_CONTEXT)

        assert mail.outbox == []
        assert set(NotificationDelivery.objects.values_list("status", flat=True)) == {DeliveryStatus.QUEUED}
        assert len(queued_actions()) == 2

    def test_the_payload_is_an_id_not_a_rendered_email(self, tenancy):
        notify(tenancy.workspace, "flow_loop_cap_hit", context=LOOP_CAP_CONTEXT)

        payload = queued_actions()[0].payload
        assert set(payload) == {"delivery_id"}
        assert NotificationDelivery.objects.filter(pk=payload["delivery_id"]).exists()

    def test_the_idempotency_key_makes_a_double_enqueue_impossible(self, tenancy):
        """SPEC §5 puts a UNIQUE on scheduled_action.idempotency_key, so the
        database refuses the second insert rather than sending twice."""
        notify(tenancy.workspace, "flow_loop_cap_hit", context=LOOP_CAP_CONTEXT)
        actions = queued_actions()

        keys = [action.idempotency_key for action in actions]
        assert len(keys) == len(set(keys))
        for action in actions:
            assert action.idempotency_key.endswith(action.payload["delivery_id"])

        # And enqueueing the same delivery again returns the row that won
        # rather than writing a second one.
        delivery = NotificationDelivery.objects.get(pk=actions[0].payload["delivery_id"])
        assert queue.enqueue_email(delivery, workspace=tenancy.workspace) is True
        assert len(queued_actions()) == len(actions)

    def test_the_workspace_travels_with_the_action(self, tenancy):
        notify(tenancy.workspace, "flow_loop_cap_hit", context=LOOP_CAP_CONTEXT)

        assert {action.workspace_id for action in queued_actions()} == {tenancy.workspace.pk}

    def test_an_account_level_notification_becomes_a_system_row(self, tenancy):
        """No workspace means schedule_system, whose rows carry a NULL workspace.
        #5's schedule() has no `workspace=None` mode and says so."""
        delivery = NotificationDelivery.objects.create(
            notification=notify(
                tenancy.workspace, "flow_loop_cap_hit", users=[tenancy.owner], context=LOOP_CAP_CONTEXT
            )[0],
            channel="email",
            status=DeliveryStatus.PENDING,
        )
        ScheduledAction.objects.unscoped().filter(type=queue.HANDLER_TYPE).delete()

        assert queue.enqueue_email(delivery, workspace=None) is True
        assert queued_actions()[0].workspace_id is None

    def test_the_lookup_happens_once_per_process(self, tenancy):
        """A 500-recipient fan-out must not re-resolve the entry point 500 times."""
        with patch("apps.notifications.queue.importlib.import_module", wraps=queue.importlib.import_module) as imported:
            notify(tenancy.workspace, "flow_loop_cap_hit", context=LOOP_CAP_CONTEXT)
            notify(tenancy.workspace, "flow_loop_cap_hit", context=LOOP_CAP_CONTEXT)

        assert len(queued_actions()) == 4
        assert imported.call_count == 1

    def test_the_worker_handler_sends_the_email(self, tenancy):
        notify(tenancy.workspace, "flow_loop_cap_hit", context=LOOP_CAP_CONTEXT)
        action = queued_actions()[0]

        queue.handle_notification_email(action.payload, action)

        assert len(mail.outbox) == 1
        assert NotificationDelivery.objects.get(pk=action.payload["delivery_id"]).status == DeliveryStatus.SENT

    def test_the_registered_handler_accepts_the_signature_the_worker_calls(self, tenancy):
        """#5's Handler is Callable[[dict, ScheduledAction], None]. Registering a
        one-argument function would only fail when a real action came due."""
        notify(tenancy.workspace, "flow_loop_cap_hit", context=LOOP_CAP_CONTEXT)
        action = queued_actions()[0]

        get_handler(queue.HANDLER_TYPE)(action.payload, action)

        assert len(mail.outbox) == 1

    def test_a_deleted_delivery_is_a_no_op_not_a_failed_action(self, tenancy, caplog):
        """The notification was deleted between enqueue and run. Failing the
        action would retry it five times to reach the same conclusion."""
        with caplog.at_level("INFO", logger="apps.notifications.queue"):
            queue.handle_notification_email({"delivery_id": "00000000-0000-0000-0000-000000000000"})

        assert mail.outbox == []
        assert "no longer exists" in caplog.text


@pytest.mark.django_db
class TestASignatureMismatchIsLoud:
    """The old behaviour here was to catch the TypeError, log, and send inline.

    That is precisely what hid the real mismatch for a whole layer: email kept
    arriving, so nothing went red, and the queue was simply never used. Losing
    an email is bad; losing the queue for every email, invisibly, is worse.
    """

    def test_a_missing_entry_point_raises_rather_than_degrading(self):
        with patch("apps.notifications.queue.importlib.import_module") as imported:
            imported.return_value = object()  # a module exporting nothing

            with pytest.raises(AttributeError, match="apps/notifications/queue.py"):
                queue.queueing_available()

    def test_a_rejected_call_signature_raises(self, tenancy):
        with patch("apps.queueing.registry.schedule", side_effect=TypeError("unexpected keyword")):
            queue.reset_bindings()
            with pytest.raises(TypeError):
                queue.enqueue_email(
                    NotificationDelivery(pk="00000000-0000-0000-0000-000000000001"), workspace=tenancy.workspace
                )


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

    @staticmethod
    def _one_payload(tenancy):
        notify(tenancy.workspace, "flow_loop_cap_hit", users=[tenancy.owner], context=LOOP_CAP_CONTEXT)
        return queued_actions()[0].payload

    def test_a_failed_send_raises_so_the_queue_retries(self, tenancy):
        payload = self._one_payload(tenancy)

        with (
            patch("django.core.mail.EmailMultiAlternatives.send", side_effect=smtplib.SMTPException("greylisted")),
            pytest.raises(queue.NotificationEmailError, match="failed"),
        ):
            queue.handle_notification_email(payload)

        assert NotificationDelivery.objects.get(pk=payload["delivery_id"]).status == DeliveryStatus.FAILED

    def test_a_successful_send_returns_quietly(self, tenancy):
        payload = self._one_payload(tenancy)

        assert queue.handle_notification_email(payload) is None
        assert len(mail.outbox) == 1

    def test_an_already_sent_delivery_is_not_mailed_twice(self, tenancy):
        """The idempotency key stops a second action *row*; it does nothing
        about one row running twice, which is what zombie recovery does after a
        worker sends the mail and dies before marking the action done."""
        payload = self._one_payload(tenancy)
        queue.handle_notification_email(payload)
        assert len(mail.outbox) == 1

        queue.handle_notification_email(payload)

        assert len(mail.outbox) == 1

    def test_a_retry_after_a_failure_is_still_attempted(self, tenancy):
        """Only SENT is skipped — a FAILED row is exactly what a retry is for."""
        payload = self._one_payload(tenancy)
        with (
            patch("django.core.mail.EmailMultiAlternatives.send", side_effect=smtplib.SMTPException("boom")),
            pytest.raises(queue.NotificationEmailError),
        ):
            queue.handle_notification_email(payload)

        queue.handle_notification_email(payload)

        assert len(mail.outbox) == 1
        assert NotificationDelivery.objects.get(pk=payload["delivery_id"]).status == DeliveryStatus.SENT


@pytest.mark.django_db(transaction=True)
class TestEndToEndThroughTheRealWorker:
    """The whole path, with nothing simulated.

    Every other test here calls one end of the seam. This one runs #5's actual
    worker over the row #7 enqueued, which is the only check that would have
    caught the registration defect: the handler has to be *findable by type* and
    *callable with the worker's signature*, and neither is observable from
    either side alone.

    ``transaction=True`` because ``claim_batch`` documents that it must run in
    autocommit — a claim rolled back with its handler is the lost update the
    whole design exists to prevent.
    """

    def test_notify_enqueues_and_the_worker_sends(self, tenancy):
        from apps.queueing.worker import run_batch

        notify(tenancy.workspace, "flow_loop_cap_hit", users=[tenancy.owner], context=LOOP_CAP_CONTEXT)

        action = ScheduledAction.objects.unscoped().get(type=queue.HANDLER_TYPE)
        assert action.workspace_id == tenancy.workspace.pk
        assert action.status == "pending"
        assert mail.outbox == []
        assert NotificationDelivery.objects.get().status == DeliveryStatus.QUEUED

        # Nudge the row unambiguously into the past before draining. `run_at` is
        # stamped from Python's clock (`queue.enqueue_email`) and the claim
        # compares it against Postgres's `now()`, and the two are not the same
        # clock: this database reads a fraction of a millisecond behind the test
        # process, so a row scheduled for "now" is intermittently not yet due and
        # the batch comes back empty. The test is about the handler being
        # findable and callable, not about sub-millisecond due-ness.
        ScheduledAction.objects.unscoped().filter(pk=action.pk).update(run_at=timezone.now() - timedelta(seconds=1))

        result = run_batch()

        action.refresh_from_db()
        assert (result.claimed, result.done, result.failed) == (1, 1, 0)
        assert action.status == "done"
        assert len(mail.outbox) == 1
        assert NotificationDelivery.objects.get().status == DeliveryStatus.SENT
