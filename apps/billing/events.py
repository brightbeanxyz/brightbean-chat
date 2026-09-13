"""What each Stripe event does to a ``BillingCustomer``.

**Stripe guarantees neither delivery nor order.** ``customer.subscription.updated``
routinely arrives before the ``created`` it supersedes, a delivery can be
retried hours later, and one can be dropped entirely. Three arrangements make
that survivable, and each is doing a different job:

*The dedup insert.* ``StripeEventLog.event_id`` is unique, so the first thing
:func:`handle` does is try to claim the event. A second delivery loses the race
and returns without touching anything. This is ``channels.WebhookEventLog``'s
mechanism, for the same reason.

*One writer for subscription state.* ``checkout.session.completed`` does exactly
one thing — bind the customer id to the organization — and writes **no**
subscription fields. Everything about the subscription comes from
``customer.subscription.*``. With two writers there are two orderings to reason
about; with one there is one.

*A timestamp guard.* A subscription event is applied only when its
``created`` is at least as new as the last one applied. A stale event is recorded
and dropped rather than being allowed to resurrect an old state.

None of that helps with an event that never arrives, which is why
``apps.billing.housekeeping.reconcile_pending_checkouts`` exists. It is worth
more than any amount of ordering cleverness, because it repairs the one failure
the ordering rules cannot see.

**A webhook never creates an organization.** An event naming a customer this
deployment has never stored is recorded as ``ignored`` and answered 200 — one
Stripe account can serve more than one deployment, and inventing a tenant from an
unauthenticated message is not a thing this should be able to do.
"""

import logging
from datetime import UTC, datetime
from typing import Any

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.billing.models import MAX_RAW_BYTES, STATUS_NONE, BillingCustomer, EventStatus, StripeEventLog
from apps.channels import security

logger = logging.getLogger(__name__)

SUBSCRIPTION_EVENTS = frozenset(
    {"customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted"}
)
HANDLED_EVENTS = SUBSCRIPTION_EVENTS | {"checkout.session.completed", "customer.deleted"}


class CustomerConflictError(Exception):
    """A binding would move a Stripe customer between organizations.

    The one way this integration could become a cross-tenant bug, so it is an
    exception rather than a log line: the event is marked ``failed`` and a human
    has to look at it.
    """


def handle(event: dict[str, Any]) -> str:
    """Apply one verified event. Returns the status recorded for it.

    Raises only for a genuine handling failure; the caller answers 200 either
    way and logs. See ``apps/billing/views_webhooks.py`` on why a 5xx here would
    take billing down for everybody.
    """
    event_id = str(event.get("id") or "")
    event_type = str(event.get("type") or "")

    row = _claim(event_id, event_type, event)
    if row is None:
        # Another delivery of the same event already did this.
        logger.debug("Ignoring duplicate Stripe event %s", event_id)
        return EventStatus.PROCESSED

    if event_type not in HANDLED_EVENTS:
        # Not an error. Stripe sends whatever the endpoint is subscribed to, and
        # answering 4xx to an unknown type is how an endpoint gets disabled.
        _finish(row, EventStatus.IGNORED)
        return EventStatus.IGNORED

    try:
        customer = _dispatch(event_type, event)
    except Exception:
        _finish(row, EventStatus.FAILED)
        raise

    row.customer = customer
    _finish(row, EventStatus.IGNORED if customer is None else EventStatus.PROCESSED)
    return row.status


def _claim(event_id: str, event_type: str, event: dict[str, Any]) -> StripeEventLog | None:
    """Insert the event row, or return None if it was already there.

    Its own transaction: an IntegrityError marks the surrounding one unusable,
    so without this the duplicate path could not go on to read anything.
    """
    try:
        with transaction.atomic():
            return StripeEventLog.objects.create(
                event_id=event_id,
                event_type=event_type,
                raw=_storable(event),
            )
    except IntegrityError:
        return None


def _storable(event: dict[str, Any]) -> dict[str, Any]:
    """The payload as stored: null-scrubbed and size-capped.

    ``scrub_nulls`` because Postgres refuses a ``\\x00`` in a text value and a
    webhook is attacker-reachable content even when it is signed. The cap
    because this table is written by an endpoint we do not rate-limit by size
    beyond the body cap, and an unbounded JSON column is a disk-exhaustion
    surface.
    """
    import json

    scrubbed = security.scrub_nulls(event)
    if len(json.dumps(scrubbed)) > MAX_RAW_BYTES:
        return {
            "id": event.get("id"),
            "type": event.get("type"),
            "truncated": True,
            "note": f"payload exceeded {MAX_RAW_BYTES} bytes and was not stored",
        }
    return scrubbed


def _finish(row: StripeEventLog, status: str) -> None:
    row.status = status
    row.processed_at = timezone.now()
    row.save(update_fields=["status", "processed_at", "customer", "updated_at"])


def _dispatch(event_type: str, event: dict[str, Any]) -> BillingCustomer | None:
    obj = event.get("data", {}).get("object", {})
    if not isinstance(obj, dict):
        return None

    if event_type == "checkout.session.completed":
        return _bind_customer(obj)
    if event_type == "customer.deleted":
        return _forget_customer(obj)
    return _apply_subscription(event_type, event, obj)


def _bind_customer(session: dict[str, Any]) -> BillingCustomer | None:
    """Attach the Stripe customer to the organization that checked out.

    Writes no subscription state — see the module docstring. The organization
    comes from ``client_reference_id``, which this project set when it created
    the session, never from anything a browser sent back.
    """
    customer_id = str(session.get("customer") or "")
    organization_id = str(session.get("client_reference_id") or "") or str(
        (session.get("metadata") or {}).get("organization_id") or ""
    )
    if not customer_id or not organization_id:
        return None

    from apps.organizations.models import Organization

    organization = Organization.objects.filter(pk=organization_id).first()
    if organization is None:
        # A session for an organization this deployment does not have. One
        # Stripe account can serve several deployments; this is not ours.
        return None

    with transaction.atomic():
        existing = BillingCustomer.objects.select_for_update().filter(stripe_customer_id=customer_id).first()
        if existing is not None and str(existing.organization_id) != str(organization.pk):
            raise CustomerConflictError(f"Stripe customer {customer_id} is already bound to another organization")

        row = existing or BillingCustomer.objects.filter(organization=organization).first()
        if row is not None and row.stripe_customer_id and row.stripe_customer_id != customer_id:
            raise CustomerConflictError(f"Organization {organization.pk} already has a different Stripe customer")

        if row is None:
            row = BillingCustomer(organization=organization)
        row.stripe_customer_id = customer_id
        row.checkout_pending_since = None
        row.save()
    return row


def _forget_customer(obj: dict[str, Any]) -> BillingCustomer | None:
    """A customer was deleted in the Stripe dashboard. Cheap insurance."""
    row = _customer_for(str(obj.get("id") or ""))
    if row is None:
        return None
    row.stripe_subscription_id = ""
    row.status = STATUS_NONE
    row.cancel_at_period_end = False
    row.current_period_end = None
    row.save(
        update_fields=[
            "stripe_subscription_id",
            "status",
            "cancel_at_period_end",
            "current_period_end",
            "updated_at",
        ]
    )
    return row


def _apply_subscription(event_type: str, event: dict[str, Any], subscription: dict[str, Any]) -> BillingCustomer | None:
    """Write subscription state, unless this event is older than what we have."""
    row = _customer_for(str(subscription.get("customer") or ""))
    if row is None:
        return None

    created = _timestamp(event.get("created"))
    if created is not None and row.last_event_at is not None and created < row.last_event_at:
        # Stale. `deleted` cannot lose this comparison in practice because it is
        # always the newest event for a subscription, so no special case.
        logger.info("Ignoring out-of-order Stripe event %s for %s", event.get("id"), row.stripe_customer_id)
        return row

    _write_subscription(row, subscription, deleted=event_type == "customer.subscription.deleted")
    row.last_event_at = created or timezone.now()
    row.save()
    return row


def _write_subscription(row: BillingCustomer, subscription: dict[str, Any], *, deleted: bool) -> None:
    """Copy a subscription's state onto the row. Does not save."""
    if deleted:
        row.status = "canceled"
        row.stripe_subscription_id = ""
        row.cancel_at_period_end = False
    else:
        row.status = str(subscription.get("status") or STATUS_NONE)
        row.stripe_subscription_id = str(subscription.get("id") or "")
        row.cancel_at_period_end = bool(subscription.get("cancel_at_period_end"))
        row.price_id = _price_id(subscription)
    row.current_period_end = _period_end(subscription)
    row.checkout_pending_since = None


def apply_snapshot(row: BillingCustomer, subscription: dict[str, Any] | None) -> None:
    """Apply a subscription read directly from Stripe, and save.

    Deliberately skips the ``last_event_at`` ordering guard that
    :func:`_apply_subscription` applies. That guard exists because *events*
    arrive out of order; a snapshot is a direct read of what is true right now,
    so it is never stale and must be able to correct a row that a lost event
    left wrong. It does not advance ``last_event_at`` either — a later event
    that genuinely is newer than the last one seen should still apply.

    ``None`` means the customer has no subscription at all, which is what a
    checkout that was abandoned after the session was created looks like.
    """
    if subscription is None:
        row.status = STATUS_NONE
        row.stripe_subscription_id = ""
        row.cancel_at_period_end = False
        row.current_period_end = None
        row.checkout_pending_since = None
    else:
        _write_subscription(row, subscription, deleted=False)
    row.save()


def _customer_for(customer_id: str) -> BillingCustomer | None:
    """The row for a Stripe customer id, or None.

    A cross-tenant read by necessity: the webhook has no session and no
    organization, and this column is its only route back to a tenant. The
    lookup is an exact match on an indexed, unique column — which is also why
    ``apps/billing/models.py`` refuses to encrypt it.
    """
    if not customer_id:
        return None
    return BillingCustomer.objects.filter(stripe_customer_id=customer_id).first()


def _items(subscription: dict[str, Any]) -> list[dict[str, Any]]:
    data = (subscription.get("items") or {}).get("data") or []
    return [item for item in data if isinstance(item, dict)]


def _price_id(subscription: dict[str, Any]) -> str:
    for item in _items(subscription):
        price = item.get("price") or {}
        if isinstance(price, dict) and price.get("id"):
            return str(price["id"])
    return ""


def _period_end(subscription: dict[str, Any]) -> datetime | None:
    """When the current period ends.

    Read from the subscription **item** first and the subscription second.
    Stripe moved this field down to the item in its 2025 versions, and a
    deployment pinned either side of that change has to keep working — this is
    display copy ("renews on …"), so guessing wrong is a wrong date on a page
    rather than a wrong entitlement. Nothing gates on it; see
    ``BillingCustomer.is_entitled``.
    """
    for item in _items(subscription):
        stamp = _timestamp(item.get("current_period_end"))
        if stamp is not None:
            return stamp
    return _timestamp(subscription.get("current_period_end"))


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return None
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None
