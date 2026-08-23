"""Delivering outbound webhooks (SPEC §17).

One delivery is: build a stable JSON body, sign it, POST it **through the SSRF
guard**, and record what happened. Retries and the auto-disable counter sit on
top of that.

**Why this handler schedules its own retries.** ``apps.queueing.worker`` runs a
handler inside ``transaction.atomic()`` and rolls that transaction back when the
handler raises — which is the right default, and exactly wrong here: raising to
get a retry would also discard the delivery-log row and the failure counter that
say *why* the retry is happening. So a failed delivery returns normally, writes
its row, and enqueues the next attempt itself at ``worker.next_run_at(attempt)``.
The backoff schedule is reused, not reimplemented;
``apps.messaging.handlers.schedule_send_retry`` keeps its retry budget on the
domain row for the same reason.

**The guard is not optional** (SECURITY-BASELINE §6). A webhook URL is
operator-supplied, so every request here goes through
``apps.common.outbound.guarded_request``: DNS resolved once and the connection
pinned to the literal address, every redirect hop re-validated, body capped,
total deadline enforced. ``apps.channels.providers.base.request_json`` is the
sibling for fixed platform hosts and is the wrong tool for this.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.common.logging import scrub
from apps.common.outbound import BlockedURLError, OutboundError, guarded_request
from apps.common.signing import sign_webhook
from apps.queueing.registry import register_handler, schedule
from apps.queueing.worker import BACKOFF_SCHEDULE, next_run_at

LOG = logging.getLogger(__name__)

__all__ = [
    "ACTION_TYPE",
    "MAX_DELIVERY_ATTEMPTS",
    "SIGNATURE_HEADER",
    "TIMESTAMP_HEADER",
    "build_body",
    "deliver_once",
    "enqueue_delivery",
    "send_test_event",
]

#: Registered with the queue's handler registry. ``ActionType`` is deliberately
#: an open enum (see its docstring, which names this string) so the type lives
#: with its handler rather than in ``apps.queueing``.
ACTION_TYPE = "webhook_delivery"

#: Rebranded from SPEC §17's ``X-OpenChat-*``: "OpenChat" was the working title
#: and this repo ships as BrightBean Chat (SPEC §22). A signature header is a
#: permanent wire contract that integrators copy into their verifiers, so it
#: carries the shipping name. SPEC §17 was updated in the same change.
SIGNATURE_HEADER = "X-BrightBean-Signature"
TIMESTAMP_HEADER = "X-BrightBean-Timestamp"
EVENT_HEADER = "X-BrightBean-Event"
DELIVERY_HEADER = "X-BrightBean-Delivery"

#: One more than the backoff schedule has rungs: the first try plus one per
#: delay. Mirrors ``apps.messaging.handlers.MAX_SEND_ATTEMPTS``.
MAX_DELIVERY_ATTEMPTS = 1 + len(BACKOFF_SCHEDULE)

_TEST_EVENT = "webhook.test"


def _jsonable(value: Any) -> Any:
    """Coerce catalog payload values into something a JSONField accepts.

    Contract 7's payloads are UUIDs and datetimes. ``ScheduledAction.payload``
    is a plain ``JSONField`` with the default encoder, so an un-coerced UUID is
    a ``TypeError`` at enqueue time — inside the emitter's transaction, which
    would take the emitting write down with it.
    """
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)


def build_body(*, delivery_id: str, event: str, workspace_id: str, occurred_at: str, data: dict[str, Any]) -> bytes:
    """The exact bytes that get signed and sent.

    ``sort_keys`` and a separator-tight dump so the same delivery produces the
    same bytes on every attempt: a receiver deduplicating on
    ``X-BrightBean-Delivery`` should see one payload, not five that differ only
    in key order.

    Ids only, never message bodies — contract 7's payloads carry no content, and
    a webhook is not a place to widen that.
    """
    document = {
        "id": delivery_id,
        "event": event,
        "workspace_id": workspace_id,
        "occurred_at": occurred_at,
        "data": _jsonable(data),
    }
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")


def enqueue_delivery(
    webhook: Any,
    *,
    event: str,
    data: dict[str, Any],
    occurred_at: Any = None,
    attempt: int = 1,
    delivery_id: str | None = None,
    run_at: Any = None,
) -> Any:
    """Queue one delivery attempt.

    No idempotency key, deliberately. The key would have to be derived from the
    event, and contract 7 emits an event per *change* — two field updates a
    microsecond apart are two deliveries, not one. A key precise enough to
    separate them is a key that never collides, which is a key that does
    nothing; a key coarse enough to collide would silently swallow the second
    change. Webhooks are at-least-once, and ``X-BrightBean-Delivery`` is what a
    receiver deduplicates on.

    ``contact`` is deliberately not passed either: a contact-bearing row is run
    under that contact's advisory lock (SPEC §9.6), and holding it across a
    ten-second call to someone else's server would stall every message for that
    contact behind a slow receiver.
    """
    payload = {
        "webhook_id": str(webhook.pk),
        "delivery_id": delivery_id or str(uuid.uuid4()),
        "event": event,
        "data": _jsonable(data),
        "occurred_at": _jsonable(occurred_at or timezone.now()),
        "attempt": attempt,
    }
    return schedule(
        ACTION_TYPE,
        run_at or timezone.now(),
        payload,
        workspace=webhook.workspace,
    )


def deliver_once(
    webhook: Any,
    *,
    delivery_id: str,
    event: str,
    data: dict[str, Any],
    occurred_at: str,
    attempt: int,
    timeout: float | None = None,
) -> Any:
    """POST one delivery and record the outcome. Never raises for a bad receiver.

    Returns the ``WebhookDelivery`` row. A non-2xx is an ordinary response to
    the guard, so it lands here as a status code rather than an exception; only
    a refused address or an unreachable host raises, and both are caught.
    """
    from apps.api.models import DeliveryStatus, WebhookDelivery

    body = build_body(
        delivery_id=delivery_id,
        event=event,
        workspace_id=str(webhook.workspace_id),
        occurred_at=occurred_at,
        data=data,
    )
    timestamp = int(timezone.now().timestamp())
    headers = {
        "Content-Type": "application/json",
        SIGNATURE_HEADER: sign_webhook(webhook.secret, timestamp=timestamp, raw_body=body),
        TIMESTAMP_HEADER: str(timestamp),
        EVENT_HEADER: event,
        DELIVERY_HEADER: delivery_id,
        "User-Agent": "BrightBeanChat-Webhook/1",
    }

    status = DeliveryStatus.FAILED
    response_code: int | None = None
    duration_ms = 0
    error = ""
    try:
        response = guarded_request(
            "POST",
            webhook.url,
            headers=headers,
            content=body,
            timeout=settings.API_WEBHOOK_TIMEOUT_SECONDS if timeout is None else timeout,
        )
    except BlockedURLError as exc:
        # The address itself is refused. Retrying repeats the same refusal, so
        # this outcome is terminal for the delivery — see the handler.
        status = DeliveryStatus.BLOCKED
        error = str(exc)
    except OutboundError as exc:
        error = str(exc)
    else:
        response_code = response.status_code
        duration_ms = response.elapsed_ms
        if response.ok:
            status = DeliveryStatus.SUCCEEDED
        else:
            error = f"Receiver answered {response.status_code}."

    return WebhookDelivery.objects.create(
        workspace_id=webhook.workspace_id,
        webhook=webhook,
        event=event,
        status=status,
        attempt=attempt,
        response_code=response_code,
        duration_ms=duration_ms,
        error=_storable(error),
    )


def _storable(message: str) -> str:
    """Scrub and cap an error before it goes anywhere near a column."""
    from apps.api.models import MAX_STORED_ERROR_CHARS

    if not message:
        return ""
    cleaned = scrub(message)
    if len(cleaned) <= MAX_STORED_ERROR_CHARS:
        return cleaned
    return cleaned[: MAX_STORED_ERROR_CHARS - 1] + "…"


def record_success(webhook: Any) -> None:
    """A delivery landed: clear the failure streak.

    No row lock, unlike :func:`record_failure`, because there is nothing to
    read: every value written here is a constant. It still serialises correctly
    against a concurrent failure — an ``UPDATE`` conflicts with that function's
    ``SELECT … FOR UPDATE`` at the row level, so one waits for the other and
    whichever lands last is the true outcome.
    """
    from apps.api.models import OutboundWebhook

    now = timezone.now()
    webhook.consecutive_failures = 0
    webhook.last_delivery_at = now
    OutboundWebhook.objects.for_workspace(webhook.workspace_id).filter(pk=webhook.pk).update(
        consecutive_failures=0, last_delivery_at=now, updated_at=now
    )


def record_failure(webhook: Any) -> bool:
    """A delivery gave up: extend the streak, and disable at the limit.

    Returns True when this failure was the one that switched the endpoint off.

    "Consecutive failures" counts *deliveries*, not HTTP attempts: the delivery
    has already been retried across the full backoff schedule by the time this
    is called, so a receiver that blips for a minute never gets here.

    **The counter is read and written under a row lock**, because this is a
    read-modify-write and SPEC §20 runs several worker processes (plus
    ``/internal/tick``) against one database. Incrementing from the caller's
    in-memory copy loses updates whenever two deliveries for the same endpoint
    finish at once: two failures from 98 both store 99, so a genuinely dead
    receiver can sit one short of the threshold indefinitely and never
    auto-disable. Re-reading inside the lock also makes the disable decision
    exactly-once, so the admin notification cannot fire twice.
    """
    from apps.api.models import OutboundWebhook

    now = timezone.now()
    limit = settings.API_WEBHOOK_MAX_CONSECUTIVE_FAILURES

    with transaction.atomic():
        locked = (
            OutboundWebhook.objects.for_workspace(webhook.workspace_id)
            .select_for_update()
            .filter(pk=webhook.pk)
            .first()
        )
        if locked is None:
            # Deleted while the delivery was in flight; nothing to count.
            return False

        failures = (locked.consecutive_failures or 0) + 1
        # Only the *transition* disables and notifies. Deliveries already in
        # flight when the threshold is crossed still land here afterwards, and
        # without the `locked.enabled` term each of them would re-disable an
        # endpoint that is already off and mail the admins about it again.
        disable = failures >= limit and locked.enabled

        fields: dict[str, Any] = {"consecutive_failures": failures, "last_delivery_at": now, "updated_at": now}
        if disable:
            fields["enabled"] = False
            fields["disabled_at"] = now

        OutboundWebhook.objects.for_workspace(webhook.workspace_id).filter(pk=webhook.pk).update(**fields)

        # Keep the caller's copy in step with what was actually stored, rather
        # than with what it guessed before the lock.
        webhook.consecutive_failures = failures
        webhook.last_delivery_at = now
        if disable:
            webhook.enabled = False
            webhook.disabled_at = now
            _notify_disabled(webhook, failures)

    return disable


def _notify_disabled(webhook: Any, failures: int) -> None:
    """Tell the workspace admins (L2-E). The event is already in the registry."""
    from apps.notifications.engine import notify

    notify(
        webhook.workspace_id,
        "outbound_webhook_disabled",
        roles=("admin",),
        context={"url": webhook.url, "failure_count": failures},
    )


@register_handler(ACTION_TYPE)
def handle_webhook_delivery(payload: dict[str, Any], action: Any) -> None:
    """Run one delivery attempt and decide what happens next.

    Returns rather than raises on a delivery failure — see the module docstring.
    A malformed payload or a vanished endpoint is a no-op: the subscription it
    belonged to is gone, and failing the row would only park an unactionable
    error in the queue.
    """
    from apps.api.models import DeliveryStatus, OutboundWebhook

    webhook_id = payload.get("webhook_id")
    event = payload.get("event") or ""
    delivery_id = payload.get("delivery_id") or str(uuid.uuid4())
    attempt = int(payload.get("attempt") or 1)
    if not webhook_id or not event:
        return

    webhook = (
        OutboundWebhook.objects.for_workspace(action.workspace_id)
        .select_related("workspace")
        .filter(pk=webhook_id)
        .first()
    )
    if webhook is None or not webhook.enabled:
        # Deleted, or switched off between enqueue and delivery. Either way the
        # operator has said they do not want this.
        return

    delivery = deliver_once(
        webhook,
        delivery_id=delivery_id,
        event=event,
        data=payload.get("data") or {},
        occurred_at=payload.get("occurred_at") or "",
        attempt=attempt,
    )

    if delivery.status == DeliveryStatus.SUCCEEDED:
        record_success(webhook)
        return

    retriable = delivery.status != DeliveryStatus.BLOCKED and attempt < MAX_DELIVERY_ATTEMPTS
    if retriable:
        enqueue_delivery(
            webhook,
            event=event,
            data=payload.get("data") or {},
            occurred_at=payload.get("occurred_at"),
            attempt=attempt + 1,
            delivery_id=delivery_id,
            run_at=next_run_at(attempt),
        )
        return

    record_failure(webhook)


def send_test_event(webhook: Any) -> Any:
    """Deliver a synthetic event now and return the row, for the settings page.

    Synchronous so the operator sees the outcome instead of a "queued" toast,
    and deliberately outside the failure accounting: a test that a receiver is
    not ready for must not push a healthy endpoint towards auto-disable, and a
    test is not retried.

    Synchronous also means this is the one delivery that occupies a web thread
    rather than a worker, so it gets its own, shorter deadline
    (``API_WEBHOOK_TEST_TIMEOUT_SECONDS``). Without that, a handful of operators
    testing dead endpoints could hold every thread the app has for the worker's
    full ten seconds each.
    """
    return deliver_once(
        webhook,
        delivery_id=str(uuid.uuid4()),
        event=_TEST_EVENT,
        data={"message": "This is a test delivery from BrightBean Chat."},
        occurred_at=timezone.now().isoformat(),
        attempt=1,
        timeout=settings.API_WEBHOOK_TEST_TIMEOUT_SECONDS,
    )
