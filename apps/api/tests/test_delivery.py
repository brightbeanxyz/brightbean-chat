"""Delivering outbound webhooks: signature, guard, retries and auto-disable.

The four claims SPEC §17 and the issue's acceptance criteria make, each with a
test that would fail if the implementation quietly stopped meeting it.
"""

import hashlib
import hmac
import json
import time
from datetime import timedelta

import httpx
import pytest
from django.utils import timezone

from apps.api.delivery import (
    ACTION_TYPE,
    DELIVERY_HEADER,
    EVENT_HEADER,
    MAX_DELIVERY_ATTEMPTS,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    build_body,
    enqueue_delivery,
    handle_webhook_delivery,
    send_test_event,
)
from apps.api.models import DeliveryStatus, WebhookDelivery
from apps.api.tests.support import PUBLIC, RECEIVER, FakeInternet, refusing, serving
from apps.common.outbound import reset_deployment_cache
from apps.common.signing import sign_webhook, webhook_signature_matches
from apps.queueing.models import ActionStatus, ScheduledAction
from tests.ssrf import guard_required


@pytest.fixture(autouse=True)
def _clear_deployment_cache():
    """The guard caches its own host's addresses; these tests swap the resolver."""
    reset_deployment_cache()
    yield
    reset_deployment_cache()


def run_pending(workspace):
    """Run every queued delivery once, the way the worker would."""
    actions = list(
        ScheduledAction.objects.for_workspace(workspace).filter(type=ACTION_TYPE, status=ActionStatus.PENDING)
    )
    for action in actions:
        action.status = ActionStatus.DONE
        action.save(update_fields=["status"])
        handle_webhook_delivery(action.payload, action)
    return actions


# ---------------------------------------------------------------------------
# The published verifier, executed
# ---------------------------------------------------------------------------


def documented_verifier(secret: str, headers, raw_body: bytes, tolerance: int = 300) -> bool:
    """A transcription of the snippet published in ``docs/api/v1.md``.

    Written out here rather than imported so the test exercises what a
    *receiver* would write from the documentation, not our own helper. A
    documented verifier nobody runs is a documented verifier that is eventually
    wrong.
    """
    timestamp = headers[TIMESTAMP_HEADER]
    presented = headers[SIGNATURE_HEADER]
    if abs(time.time() - int(timestamp)) > tolerance:
        return False
    signed = timestamp.encode() + b"." + raw_body
    expected = "v1=" + hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, presented)


class TestSignature:
    def test_the_documented_verifier_accepts_a_real_delivery(self):
        secret = "s3cret-value"
        body = b'{"event":"contact.created"}'
        timestamp = int(time.time())
        headers = {
            TIMESTAMP_HEADER: str(timestamp),
            SIGNATURE_HEADER: sign_webhook(secret, timestamp=timestamp, raw_body=body),
        }

        assert documented_verifier(secret, headers, body)

    def test_a_tampered_body_is_rejected(self):
        secret = "s3cret-value"
        body = b'{"amount":10}'
        timestamp = int(time.time())
        headers = {
            TIMESTAMP_HEADER: str(timestamp),
            SIGNATURE_HEADER: sign_webhook(secret, timestamp=timestamp, raw_body=body),
        }

        assert not documented_verifier(secret, headers, b'{"amount":1000}')

    def test_a_tampered_timestamp_is_rejected(self):
        """The timestamp is signed, not merely sent — that is what stops a replay."""
        secret = "s3cret-value"
        body = b"{}"
        timestamp = int(time.time())
        signature = sign_webhook(secret, timestamp=timestamp, raw_body=body)

        assert not documented_verifier(
            secret, {TIMESTAMP_HEADER: str(timestamp + 1), SIGNATURE_HEADER: signature}, body
        )

    def test_a_stale_timestamp_is_rejected_by_the_tolerance(self):
        secret = "s3cret-value"
        body = b"{}"
        old = int(time.time()) - 4000
        headers = {
            TIMESTAMP_HEADER: str(old),
            SIGNATURE_HEADER: sign_webhook(secret, timestamp=old, raw_body=body),
        }

        assert not documented_verifier(secret, headers, body)

    def test_the_wrong_secret_is_rejected(self):
        body = b"{}"
        timestamp = int(time.time())
        headers = {
            TIMESTAMP_HEADER: str(timestamp),
            SIGNATURE_HEADER: sign_webhook("right", timestamp=timestamp, raw_body=body),
        }

        assert not documented_verifier("wrong", headers, body)

    def test_our_helper_and_the_documented_verifier_agree(self):
        secret = "s3cret-value"
        body = b'{"a":1}'
        timestamp = int(time.time())
        presented = sign_webhook(secret, timestamp=timestamp, raw_body=body)

        assert webhook_signature_matches(secret, timestamp=timestamp, raw_body=body, presented=presented)
        assert not webhook_signature_matches(secret, timestamp=timestamp, raw_body=b"{}", presented=presented)


class TestBody:
    def test_the_bytes_are_stable_across_attempts(self):
        """A retry must send the same document, not a reshuffled one.

        A receiver deduplicating on the delivery id should see one payload, not
        five that differ only in key order.
        """
        kwargs = {
            "delivery_id": "d1",
            "event": "contact.created",
            "workspace_id": "w1",
            "occurred_at": "2026-08-23T09:00:00+00:00",
            "data": {"contact_id": "c1", "source": "api"},
        }

        assert build_body(**kwargs) == build_body(**kwargs)

    def test_it_carries_ids_and_no_content(self):
        document = json.loads(
            build_body(
                delivery_id="d1",
                event="message.received",
                workspace_id="w1",
                occurred_at="2026-08-23T09:00:00+00:00",
                data={"contact_id": "c1", "message_id": "m1"},
            )
        )

        assert set(document) == {"id", "event", "workspace_id", "occurred_at", "data"}
        assert document["data"] == {"contact_id": "c1", "message_id": "m1"}


@pytest.mark.django_db
class TestDelivery:
    def test_a_successful_delivery_is_signed_guarded_and_logged(self, tenancy, webhook, monkeypatch):
        internet = FakeInternet(serving(200)).install(monkeypatch)
        enqueue_delivery(webhook, event="contact.created", data={"contact_id": "c1"})

        with guard_required() as guarded:
            run_pending(tenancy.workspace)

        # SECURITY-BASELINE §6: proving the guard is in the path, not that a
        # patched guard was called.
        assert len(guarded) == 1
        assert internet.requests[0].url.host == PUBLIC, "pinned to the checked address"
        assert internet.requests[0].headers["host"] == RECEIVER

        sent = internet.requests[0]
        assert documented_verifier(webhook.secret, sent.headers, sent.content)
        assert sent.headers[EVENT_HEADER] == "contact.created"
        assert sent.headers[DELIVERY_HEADER]

        delivery = WebhookDelivery.objects.for_workspace(tenancy.workspace).get()
        assert delivery.status == DeliveryStatus.SUCCEEDED
        assert delivery.response_code == 200
        webhook.refresh_from_db()
        assert webhook.consecutive_failures == 0
        assert webhook.last_delivery_at is not None

    def test_a_localhost_target_is_refused_by_the_guard(self, tenancy, webhook, monkeypatch):
        """Issue #25's acceptance criterion, and SECURITY-BASELINE §6's whole point."""
        internet = FakeInternet(serving(200), names={RECEIVER: ["127.0.0.1"]}).install(monkeypatch)
        enqueue_delivery(webhook, event="contact.created", data={"contact_id": "c1"})

        run_pending(tenancy.workspace)

        assert internet.requests == [], "nothing left the process"
        delivery = WebhookDelivery.objects.for_workspace(tenancy.workspace).get()
        assert delivery.status == DeliveryStatus.BLOCKED
        assert delivery.response_code is None

    def test_a_metadata_service_target_is_refused(self, tenancy, webhook, monkeypatch):
        """169.254.169.254 is how an SSRF becomes stolen cloud credentials."""
        internet = FakeInternet(serving(200), names={RECEIVER: ["169.254.169.254"]}).install(monkeypatch)
        enqueue_delivery(webhook, event="contact.created", data={"contact_id": "c1"})

        run_pending(tenancy.workspace)

        assert internet.requests == []
        assert WebhookDelivery.objects.for_workspace(tenancy.workspace).get().status == DeliveryStatus.BLOCKED

    def test_a_blocked_address_is_not_retried(self, tenancy, webhook, monkeypatch):
        """Retrying repeats the same refusal; it is terminal for the delivery."""
        FakeInternet(serving(200), names={RECEIVER: ["10.0.0.5"]}).install(monkeypatch)
        enqueue_delivery(webhook, event="contact.created", data={"contact_id": "c1"})

        run_pending(tenancy.workspace)

        assert (
            not ScheduledAction.objects.for_workspace(tenancy.workspace)
            .filter(type=ACTION_TYPE, status=ActionStatus.PENDING)
            .exists()
        )
        webhook.refresh_from_db()
        assert webhook.consecutive_failures == 1

    def test_a_5xx_is_retried_on_the_standard_backoff(self, tenancy, webhook, monkeypatch):
        FakeInternet(serving(503)).install(monkeypatch)
        enqueue_delivery(webhook, event="contact.created", data={"contact_id": "c1"})

        run_pending(tenancy.workspace)

        retry = ScheduledAction.objects.for_workspace(tenancy.workspace).get(
            type=ACTION_TYPE, status=ActionStatus.PENDING
        )
        assert retry.payload["attempt"] == 2
        assert retry.run_at > timezone.now() + timedelta(seconds=10)
        # The delivery id is preserved so a receiver can deduplicate across
        # attempts.
        first = WebhookDelivery.objects.for_workspace(tenancy.workspace).get()
        assert first.status == DeliveryStatus.FAILED
        assert first.response_code == 503

    def test_a_transport_failure_is_retried(self, tenancy, webhook, monkeypatch):
        FakeInternet(refusing(httpx.ConnectError("nope"))).install(monkeypatch)
        enqueue_delivery(webhook, event="contact.created", data={"contact_id": "c1"})

        run_pending(tenancy.workspace)

        assert (
            ScheduledAction.objects.for_workspace(tenancy.workspace)
            .filter(type=ACTION_TYPE, status=ActionStatus.PENDING)
            .exists()
        )

    def test_retries_stop_after_the_backoff_schedule_is_exhausted(self, tenancy, webhook, monkeypatch):
        FakeInternet(serving(500)).install(monkeypatch)
        enqueue_delivery(webhook, event="contact.created", data={"contact_id": "c1"})

        for _ in range(MAX_DELIVERY_ATTEMPTS + 2):
            actions = ScheduledAction.objects.for_workspace(tenancy.workspace).filter(
                type=ACTION_TYPE, status=ActionStatus.PENDING
            )
            if not actions.exists():
                break
            for action in list(actions):
                action.status = ActionStatus.DONE
                action.save(update_fields=["status"])
                handle_webhook_delivery(action.payload, action)

        assert WebhookDelivery.objects.for_workspace(tenancy.workspace).count() == MAX_DELIVERY_ATTEMPTS
        webhook.refresh_from_db()
        assert webhook.consecutive_failures == 1, "one exhausted delivery, not one per attempt"

    def test_a_disabled_endpoint_is_skipped(self, tenancy, webhook, monkeypatch):
        internet = FakeInternet(serving(200)).install(monkeypatch)
        enqueue_delivery(webhook, event="contact.created", data={"contact_id": "c1"})
        webhook.enabled = False
        webhook.save(update_fields=["enabled"])

        run_pending(tenancy.workspace)

        assert internet.requests == []
        assert not WebhookDelivery.objects.for_workspace(tenancy.workspace).exists()

    def test_a_deleted_endpoint_is_a_no_op_rather_than_a_failure(self, tenancy, webhook, monkeypatch):
        FakeInternet(serving(200)).install(monkeypatch)
        action = enqueue_delivery(webhook, event="contact.created", data={"contact_id": "c1"})
        webhook.delete()

        handle_webhook_delivery(action.payload, action)  # must not raise

    def test_the_delivery_row_never_records_a_response_body(self, tenancy, webhook, monkeypatch):
        """A receiver's error page can echo a URL carrying its own token."""
        FakeInternet(serving(400, text="token=abcdef123456 is invalid")).install(monkeypatch)
        enqueue_delivery(webhook, event="contact.created", data={"contact_id": "c1"})

        run_pending(tenancy.workspace)

        delivery = WebhookDelivery.objects.for_workspace(tenancy.workspace).get()
        assert "abcdef123456" not in delivery.error


@pytest.mark.django_db
class TestAutoDisable:
    def test_it_disables_at_exactly_the_limit_and_notifies(self, tenancy, webhook, settings, monkeypatch):
        from apps.notifications.models import Notification

        settings.API_WEBHOOK_MAX_CONSECUTIVE_FAILURES = 3
        FakeInternet(serving(500)).install(monkeypatch)
        webhook.consecutive_failures = 1
        webhook.save(update_fields=["consecutive_failures"])

        from apps.api.delivery import record_failure

        assert record_failure(webhook) is False
        webhook.refresh_from_db()
        assert webhook.enabled is True
        assert webhook.consecutive_failures == 2

        assert record_failure(webhook) is True
        webhook.refresh_from_db()
        assert webhook.enabled is False
        assert webhook.disabled_at is not None
        assert webhook.consecutive_failures == 3

        notification = Notification.objects.filter(event_type="outbound_webhook_disabled")
        assert notification.exists()
        assert webhook.url in notification.first().body

    def test_one_success_resets_the_streak(self, tenancy, webhook):
        from apps.api.delivery import record_failure, record_success

        record_failure(webhook)
        record_failure(webhook)
        assert webhook.consecutive_failures == 2

        record_success(webhook)

        webhook.refresh_from_db()
        assert webhook.consecutive_failures == 0

    def test_ninety_nine_failures_do_not_disable(self, tenancy, webhook, settings):
        from apps.api.delivery import record_failure

        settings.API_WEBHOOK_MAX_CONSECUTIVE_FAILURES = 100
        for _ in range(99):
            record_failure(webhook)

        webhook.refresh_from_db()
        assert webhook.consecutive_failures == 99
        assert webhook.enabled is True

        record_failure(webhook)
        webhook.refresh_from_db()
        assert webhook.enabled is False


@pytest.mark.django_db
class TestTestEvent:
    def test_it_delivers_immediately_and_logs(self, tenancy, webhook, monkeypatch):
        internet = FakeInternet(serving(200)).install(monkeypatch)

        with guard_required():
            delivery = send_test_event(webhook)

        assert delivery.status == DeliveryStatus.SUCCEEDED
        assert internet.requests[0].headers[EVENT_HEADER] == "webhook.test"
        assert documented_verifier(webhook.secret, internet.requests[0].headers, internet.requests[0].content)

    def test_a_failing_test_does_not_count_towards_auto_disable(self, tenancy, webhook, monkeypatch):
        """A test against a receiver that is not ready yet must not switch it off."""
        FakeInternet(serving(500)).install(monkeypatch)

        send_test_event(webhook)

        webhook.refresh_from_db()
        assert webhook.consecutive_failures == 0
        assert webhook.enabled is True

    def test_a_test_is_not_retried(self, tenancy, webhook, monkeypatch):
        FakeInternet(serving(500)).install(monkeypatch)

        send_test_event(webhook)

        assert not ScheduledAction.objects.for_workspace(tenancy.workspace).filter(type=ACTION_TYPE).exists()


@pytest.mark.django_db
class TestHousekeeping:
    def test_it_keeps_only_the_newest_rows_per_endpoint(self, tenancy, webhook, settings):
        from apps.api.housekeeping import prune_webhook_deliveries

        settings.API_WEBHOOK_DELIVERY_LOG_KEEP = 5
        for index in range(12):
            WebhookDelivery.objects.create(
                workspace=tenancy.workspace,
                webhook=webhook,
                event="contact.created",
                status=DeliveryStatus.SUCCEEDED,
                response_code=200,
                attempt=index,
            )

        deleted = prune_webhook_deliveries()

        assert deleted == 7
        assert WebhookDelivery.objects.for_workspace(tenancy.workspace).count() == 5

    def test_it_is_registered_with_the_hourly_sweep(self):
        from apps.queueing.housekeeping import housekeeping_jobs

        assert "prune_webhook_deliveries" in housekeeping_jobs()
