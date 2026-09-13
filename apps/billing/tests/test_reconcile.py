"""The housekeeping that repairs what the webhook could not deliver.

Every ordering rule in ``apps.billing.events`` reasons about events that
arrived. This covers the one failure they cannot see: a delivery that was
dropped, or that failed while the database was down, leaving somebody who paid
sitting on the free plan with nothing to notice it.
"""

from datetime import timedelta
from typing import Any

import pytest
from django.utils import timezone

from apps.billing.entitlements import is_paid
from apps.billing.housekeeping import PENDING_CHECKOUT_MINUTES, prune_stripe_event_log, reconcile_pending_checkouts
from apps.billing.models import STATUS_NONE, BillingCustomer, StripeEventLog
from apps.billing.tests.stripe_support import SECRET_KEY, fake_stripe

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _configured(settings: Any) -> None:
    settings.STRIPE_ENABLED = True
    settings.STRIPE_SECRET_KEY = SECRET_KEY
    settings.STRIPE_PRICE_ID_MONTHLY = "price_m"
    settings.STRIPE_PRICE_ID_YEARLY = "price_y"


def pending_row(tenancy: Any, *, minutes_ago: int) -> BillingCustomer:
    return BillingCustomer.objects.create(
        organization=tenancy.organization,
        stripe_customer_id="cus_pending",
        status=STATUS_NONE,
        checkout_pending_since=timezone.now() - timedelta(minutes=minutes_ago),
    )


def with_active_subscription(fake: Any) -> None:
    fake.reply(
        "GET",
        "/v1/customers/cus_pending",
        {
            "id": "cus_pending",
            "object": "customer",
            "subscriptions": {
                "object": "list",
                "data": [
                    {
                        "id": "sub_reconciled",
                        "object": "subscription",
                        "status": "active",
                        "cancel_at_period_end": False,
                        "items": {"data": [{"price": {"id": "price_m"}, "current_period_end": 1_790_000_000}]},
                    }
                ],
            },
        },
    )


class TestReconcile:
    def test_a_lost_webhook_is_repaired(self, tenancy: Any) -> None:
        """The whole point of the job: they paid, nothing told us, and the next
        sweep notices."""
        row = pending_row(tenancy, minutes_ago=PENDING_CHECKOUT_MINUTES + 5)
        assert is_paid(tenancy.organization) is False

        with fake_stripe(with_active_subscription):
            assert reconcile_pending_checkouts() == 1

        row.refresh_from_db()
        assert row.status == "active"
        assert row.stripe_subscription_id == "sub_reconciled"
        assert row.checkout_pending_since is None
        assert is_paid(tenancy.organization) is True

    def test_a_recent_checkout_is_left_alone(self, tenancy: Any) -> None:
        """The webhook has not had its chance yet; asking Stripe now would be a
        call per page-load's worth of noise for nothing."""
        pending_row(tenancy, minutes_ago=1)

        with fake_stripe(with_active_subscription) as fake:
            assert reconcile_pending_checkouts() == 0

        assert fake.calls == []

    def test_an_abandoned_checkout_clears_its_pending_flag(self, tenancy: Any) -> None:
        """They created a session and never paid. The row must stop claiming a
        checkout is in flight, or the page says "being activated" forever."""
        row = pending_row(tenancy, minutes_ago=PENDING_CHECKOUT_MINUTES + 5)

        def no_subscriptions(fake: Any) -> None:
            fake.reply(
                "GET",
                "/v1/customers/cus_pending",
                {"id": "cus_pending", "object": "customer", "subscriptions": {"object": "list", "data": []}},
            )

        with fake_stripe(no_subscriptions):
            reconcile_pending_checkouts()

        row.refresh_from_db()
        assert row.checkout_pending_since is None
        assert row.status == STATUS_NONE
        assert is_paid(tenancy.organization) is False

    def test_a_stripe_outage_leaves_the_row_to_be_retried(self, tenancy: Any) -> None:
        """Clearing the flag on a transient failure would turn a five-minute
        Stripe outage into a permanently unreconciled customer."""
        row = pending_row(tenancy, minutes_ago=PENDING_CHECKOUT_MINUTES + 5)

        def outage(fake: Any) -> None:
            fake.reply("GET", "/v1/customers/cus_pending", {"error": {"type": "api_error"}}, status=500)

        with fake_stripe(outage):
            assert reconcile_pending_checkouts() == 0

        row.refresh_from_db()
        assert row.checkout_pending_since is not None

    def test_it_does_nothing_when_stripe_is_unconfigured(self, tenancy: Any, settings: Any) -> None:
        settings.STRIPE_ENABLED = False
        pending_row(tenancy, minutes_ago=PENDING_CHECKOUT_MINUTES + 5)

        with fake_stripe(with_active_subscription) as fake:
            assert reconcile_pending_checkouts() == 0

        assert fake.calls == []

    def test_a_snapshot_can_correct_a_row_a_lost_event_left_wrong(self, tenancy: Any) -> None:
        """A snapshot is a direct read of what is true now, so unlike an event it
        is never stale — it must be able to overwrite a row whose last_event_at
        is newer than the state it holds."""
        row = pending_row(tenancy, minutes_ago=PENDING_CHECKOUT_MINUTES + 5)
        row.last_event_at = timezone.now()
        row.status = "incomplete"
        row.save()

        with fake_stripe(with_active_subscription):
            reconcile_pending_checkouts()

        row.refresh_from_db()
        assert row.status == "active"


class TestPrune:
    def test_it_deletes_rows_past_the_retention_window(self, settings: Any) -> None:
        settings.STRIPE_EVENT_LOG_RETENTION_DAYS = 30
        old = StripeEventLog.objects.create(event_id="evt_old", event_type="customer.deleted")
        StripeEventLog.objects.filter(pk=old.pk).update(received_at=timezone.now() - timedelta(days=31))
        StripeEventLog.objects.create(event_id="evt_new", event_type="customer.deleted")

        assert prune_stripe_event_log() == 1
        assert list(StripeEventLog.objects.values_list("event_id", flat=True)) == ["evt_new"]


class TestBothJobsAreRegistered:
    def test_the_hourly_sweep_knows_about_them(self) -> None:
        """Registration happens as an import side effect in BillingConfig.ready,
        which is the one thing that makes these run at all."""
        from apps.queueing.housekeeping import housekeeping_jobs

        names = set(housekeeping_jobs())

        assert {"prune_stripe_event_log", "reconcile_pending_checkouts"} <= names
