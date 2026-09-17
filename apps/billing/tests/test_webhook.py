"""The Stripe webhook, against everything that can be pointed at it.

This endpoint is unauthenticated and it grants paid plans. The signature is the
whole credential, so every property worth having here is a property about
refusing — and a refusal test that signs its payload *wrongly* proves nothing
about the check it claims to cover. So the stale-timestamp cases below carry a
**genuinely correct digest** and differ only in ``t``; without that they would
pass against a receiver with no timestamp check at all.

The status table this asserts is in ``apps/billing/views_webhooks.py``. The two
entries worth restating: everything signature-shaped answers **403 and looks
identical**, because a distinguishable "malformed header" reply tells an attacker
their format is right and only their secret is wrong; and a handler that raises
answers **200**, because Stripe disables an endpoint that keeps failing and one
poison event must not take billing down for every customer.
"""

import json
from typing import Any

import pytest
from django.urls import resolve, reverse

from apps.billing.models import STATUS_NONE, BillingCustomer, EventStatus, StripeEventLog
from apps.billing.tests.stripe_support import WEBHOOK_SECRET, signed_headers

pytestmark = pytest.mark.django_db

URL = "/webhooks/stripe/"


@pytest.fixture(autouse=True)
def _configured(settings: Any) -> None:
    settings.STRIPE_WEBHOOK_SECRET = WEBHOOK_SECRET
    settings.STRIPE_ENABLED = True


def body(payload: dict[str, Any]) -> bytes:
    """Exactly the bytes that get signed. Never re-serialised on the way in."""
    return json.dumps(payload, separators=(",", ":")).encode()


def event(event_type: str, obj: dict[str, Any], *, event_id: str = "evt_1", created: int = 1_760_000_000) -> dict:
    return {"id": event_id, "type": event_type, "created": created, "data": {"object": obj}}


def post(client: Any, payload: dict[str, Any], **header_kwargs: Any) -> Any:
    raw = body(payload)
    headers = signed_headers(raw, **header_kwargs)
    return client.post(URL, data=raw, content_type="application/json", **headers)


def customer_row(tenancy: Any, **kwargs: Any) -> BillingCustomer:
    return BillingCustomer.objects.create(
        organization=tenancy.organization,
        stripe_customer_id=kwargs.pop("stripe_customer_id", "cus_known"),
        **kwargs,
    )


class TestTheRouteIsReachableAtAll:
    """The regression nobody would find by reading the view.

    ``apps/channels/urls_webhooks.py`` ends in ``<str:platform>/``, which matches
    ``stripe/``. Mounted after that include, this endpoint would land in
    ``platform_webhook``, fail its enum check and 404 — with the view here
    perfectly correct and never called.
    """

    def test_the_url_resolves_to_the_billing_view_not_the_platform_catch_all(self) -> None:
        assert resolve(URL).func.__module__ == "apps.billing.views_webhooks"
        assert reverse("webhook_stripe") == URL


class TestUnconfigured:
    """No signing secret means no endpoint. Part of the AGPL promise."""

    def test_it_404s_when_no_secret_is_set(self, client: Any, settings: Any) -> None:
        settings.STRIPE_WEBHOOK_SECRET = ""

        assert post(client, event("customer.subscription.created", {})).status_code == 404

    def test_a_whitespace_secret_is_also_unset(self, client: Any, settings: Any) -> None:
        """The one-click-deploy trap: a config var set to blank is not absent."""
        settings.STRIPE_WEBHOOK_SECRET = "   "

        assert post(client, event("customer.subscription.created", {})).status_code == 404


class TestSignatureRefusals:
    """Four different failures, one indistinguishable answer."""

    def test_no_signature_header_at_all(self, client: Any) -> None:
        response = client.post(URL, data=body(event("customer.deleted", {})), content_type="application/json")

        assert response.status_code == 403

    def test_a_signature_from_the_wrong_secret(self, client: Any) -> None:
        response = post(client, event("customer.deleted", {}), secret="whsec_not_the_configured_one")

        assert response.status_code == 403

    def test_a_correct_signature_that_is_too_old(self, client: Any) -> None:
        """Signed properly, with a stale ``t``. This is the replay test, and it
        only means anything because the digest is right."""
        response = post(client, event("customer.deleted", {}), timestamp=1_000_000_000)

        assert response.status_code == 403

    def test_a_correct_signature_dated_in_the_future(self, client: Any) -> None:
        import time

        response = post(client, event("customer.deleted", {}), timestamp=int(time.time()) + 10_000)

        assert response.status_code == 403

    def test_a_truncated_body_does_not_match_its_signature(self, client: Any) -> None:
        payload = event("customer.deleted", {"id": "cus_known"})
        raw = body(payload)
        headers = signed_headers(raw)

        response = client.post(URL, data=raw[:-5], content_type="application/json", **headers)

        assert response.status_code == 403

    @pytest.mark.parametrize(
        "header",
        ["", "garbage", "t=1", "v1=abc", "t=notanumber,v1=abc", "t=1760000000", ",,,", "t=1760000000,v0=abc"],
    )
    def test_every_malformed_header_shape(self, client: Any, header: str) -> None:
        response = client.post(
            URL,
            data=body(event("customer.deleted", {})),
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE=header,
        )

        assert response.status_code == 403

    def test_the_refusals_are_byte_identical(self, client: Any) -> None:
        """No oracle. A caller learns whether they hold the secret, and nothing
        else — not whether their header shape was right."""
        raw = body(event("customer.deleted", {}))
        bodies = set()
        for headers in (
            {},
            {"HTTP_STRIPE_SIGNATURE": "garbage"},
            signed_headers(raw, secret="whsec_wrong"),
            signed_headers(raw, timestamp=1_000_000_000),
        ):
            response = client.post(URL, data=raw, content_type="application/json", **headers)
            bodies.add((response.status_code, response.content))

        assert len(bodies) == 1


class TestSignatureAcceptance:
    def test_multiple_v1_values_with_one_correct_is_accepted(self, client: Any) -> None:
        """What a secret rotation looks like on the wire. Stripe sends one
        signature per active secret, and stopping at the first breaks every
        rotation."""
        response = post(client, event("customer.deleted", {"id": "cus_unknown"}), extra_v1=("deadbeef", "cafebabe"))

        assert response.status_code == 200

    def test_a_v0_alongside_a_good_v1_is_accepted(self, client: Any) -> None:
        raw = body(event("customer.deleted", {"id": "cus_unknown"}))
        headers = signed_headers(raw)
        headers["HTTP_STRIPE_SIGNATURE"] = headers["HTTP_STRIPE_SIGNATURE"].replace("t=", "v0=legacy,t=", 1)

        response = client.post(URL, data=raw, content_type="application/json", **headers)

        assert response.status_code == 200


class TestBodyShape:
    def test_an_oversized_body_is_refused_before_anything_reads_it(self, client: Any, settings: Any) -> None:
        settings.WEBHOOK_MAX_BODY_BYTES = 10

        response = post(client, event("customer.deleted", {"id": "cus_known"}))

        assert response.status_code == 413

    def test_a_verified_body_that_is_not_json(self, client: Any) -> None:
        raw = b"this is not json"
        response = client.post(URL, data=raw, content_type="application/json", **signed_headers(raw))

        assert response.status_code == 400

    @pytest.mark.parametrize("payload", [[], "a string", 42, {"type": "customer.deleted"}, {"id": "", "type": "x"}])
    def test_a_verified_body_that_is_not_an_event(self, client: Any, payload: Any) -> None:
        """Only somebody holding the signing secret can reach this, so it is a
        bug or a Stripe change rather than an attack."""
        raw = json.dumps(payload, separators=(",", ":")).encode()

        response = client.post(URL, data=raw, content_type="application/json", **signed_headers(raw))

        assert response.status_code == 400


class TestDeduplication:
    def test_the_same_event_twice_is_answered_twice_and_applied_once(self, client: Any, tenancy: Any) -> None:
        row = customer_row(tenancy, status=STATUS_NONE)
        payload = event(
            "customer.subscription.updated",
            {"id": "sub_1", "customer": "cus_known", "status": "active", "cancel_at_period_end": False},
        )

        first = post(client, payload)
        # A later delivery of the same event, and the state has moved on since.
        row.refresh_from_db()
        row.status = "canceled"
        row.save(update_fields=["status"])
        second = post(client, payload)

        assert (first.status_code, second.status_code) == (200, 200)
        assert StripeEventLog.objects.filter(event_id="evt_1").count() == 1
        row.refresh_from_db()
        assert row.status == "canceled", "a duplicate delivery re-applied its payload"


class TestUnknownEvents:
    def test_an_unknown_type_is_recorded_and_answered_200(self, client: Any) -> None:
        """4xx-ing an unknown type is how an endpoint gets disabled at Stripe."""
        response = post(client, event("invoice.payment_succeeded", {"id": "in_1"}))

        assert response.status_code == 200
        assert StripeEventLog.objects.get(event_id="evt_1").status == EventStatus.IGNORED

    def test_an_unknown_customer_creates_nothing(self, client: Any) -> None:
        """One Stripe account can serve several deployments. A webhook must never
        invent a tenant."""
        response = post(
            client,
            event("customer.subscription.updated", {"id": "sub_x", "customer": "cus_nobody", "status": "active"}),
        )

        assert response.status_code == 200
        assert BillingCustomer.objects.count() == 0
        assert StripeEventLog.objects.get(event_id="evt_1").status == EventStatus.IGNORED


class TestHandlerFailure:
    def test_a_raising_handler_still_answers_200_and_marks_the_row(self, client: Any, monkeypatch: Any) -> None:
        from apps.billing import events as events_module

        def boom(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("handler exploded")

        monkeypatch.setattr(events_module, "_dispatch", boom)

        response = post(client, event("customer.deleted", {"id": "cus_known"}))

        assert response.status_code == 200
        assert StripeEventLog.objects.get(event_id="evt_1").status == EventStatus.FAILED


class TestAFailedEventIsRetried:
    """Stripe gets one more chance, because it will not offer a third.

    The view answers 200 even when a handler raises — a 5xx makes Stripe retry a
    poison event until it disables the endpoint. So a row left FAILED has to let
    the *next* delivery through: treating it as a duplicate meant a transient
    failure lost a subscription update permanently.
    """

    def test_a_redelivery_after_a_failure_is_processed(self, client: Any, tenancy: Any, monkeypatch: Any) -> None:
        from apps.billing import events as events_module

        customer_row(tenancy, status=STATUS_NONE)
        payload = event(
            "customer.subscription.updated",
            {"id": "sub_1", "customer": "cus_known", "status": "active", "cancel_at_period_end": False},
        )

        original = events_module._dispatch
        calls: list[int] = []

        def fail_once(*args: Any, **kwargs: Any) -> Any:
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("transient")
            return original(*args, **kwargs)

        monkeypatch.setattr(events_module, "_dispatch", fail_once)

        first = post(client, payload)
        assert first.status_code == 200
        assert StripeEventLog.objects.get(event_id="evt_1").status == EventStatus.FAILED

        second = post(client, payload)

        assert second.status_code == 200
        assert StripeEventLog.objects.get(event_id="evt_1").status == EventStatus.PROCESSED
        assert BillingCustomer.objects.get(stripe_customer_id="cus_known").status == "active"

    def test_a_redelivery_after_success_is_still_a_duplicate(self, client: Any, tenancy: Any) -> None:
        """The dedup still holds for the case it was written for."""
        row = customer_row(tenancy, status=STATUS_NONE)
        payload = event(
            "customer.subscription.updated",
            {"id": "sub_1", "customer": "cus_known", "status": "active", "cancel_at_period_end": False},
        )

        post(client, payload)
        row.refresh_from_db()
        row.status = "canceled"
        row.save(update_fields=["status"])
        post(client, payload)

        row.refresh_from_db()
        assert row.status == "canceled", "a duplicate delivery re-applied its payload"


class TestADeletedSubscriptionDoesNotLookLikeARenewal:
    def test_the_period_end_is_cleared_on_deletion(self, client: Any, tenancy: Any) -> None:
        """Stripe sends the final period on the deletion event, and keeping it
        made the page tell a cancelled organization it renews."""
        from apps.billing.selectors import billing_context

        customer_row(tenancy, status="active", stripe_subscription_id="sub_1")

        post(
            client,
            event(
                "customer.subscription.deleted",
                {"id": "sub_1", "customer": "cus_known", "current_period_end": 1_790_000_000},
            ),
        )

        row = BillingCustomer.objects.get(stripe_customer_id="cus_known")
        assert row.current_period_end is None
        assert billing_context(tenancy.organization)["renews_on"] is None


class TestCrossTenant:
    def test_a_customer_cannot_be_moved_between_organizations(
        self, client: Any, tenancy: Any, other_tenancy: Any
    ) -> None:
        """The one path by which this becomes a cross-tenant bug."""
        customer_row(tenancy, stripe_customer_id="cus_shared", status="active")

        response = post(
            client,
            event(
                "checkout.session.completed",
                {"customer": "cus_shared", "client_reference_id": str(other_tenancy.organization.pk)},
            ),
        )

        assert response.status_code == 200
        assert StripeEventLog.objects.get(event_id="evt_1").status == EventStatus.FAILED
        assert BillingCustomer.objects.get(stripe_customer_id="cus_shared").organization_id == tenancy.organization.pk
        assert not BillingCustomer.objects.filter(organization=other_tenancy.organization).exists()


class TestOrdering:
    def test_a_stale_subscription_event_does_not_resurrect_an_old_state(self, client: Any, tenancy: Any) -> None:
        """Stripe guarantees no ordering, and `updated` routinely lands before
        the `created` it supersedes."""
        row = customer_row(tenancy, status=STATUS_NONE)

        post(
            client,
            event(
                "customer.subscription.updated",
                {"id": "sub_1", "customer": "cus_known", "status": "active"},
                event_id="evt_new",
                created=1_760_000_500,
            ),
        )
        post(
            client,
            event(
                "customer.subscription.created",
                {"id": "sub_1", "customer": "cus_known", "status": "incomplete"},
                event_id="evt_old",
                created=1_760_000_000,
            ),
        )

        row.refresh_from_db()
        assert row.status == "active", "an older event overwrote a newer one"

    def test_checkout_completed_writes_no_subscription_state(self, client: Any, tenancy: Any) -> None:
        """One writer for subscription fields. Binding is all this event does."""
        response = post(
            client,
            event(
                "checkout.session.completed",
                {"customer": "cus_fresh", "client_reference_id": str(tenancy.organization.pk), "status": "complete"},
            ),
        )

        assert response.status_code == 200
        row = BillingCustomer.objects.get(organization=tenancy.organization)
        assert row.stripe_customer_id == "cus_fresh"
        assert row.status == STATUS_NONE, "checkout.session.completed wrote subscription state"
        assert row.checkout_pending_since is None


class TestSubscriptionLifecycle:
    def test_an_active_subscription_entitles(self, client: Any, tenancy: Any) -> None:
        from apps.billing.entitlements import is_paid

        customer_row(tenancy, status=STATUS_NONE)

        post(
            client,
            event(
                "customer.subscription.created",
                {
                    "id": "sub_1",
                    "customer": "cus_known",
                    "status": "active",
                    "cancel_at_period_end": False,
                    "items": {"data": [{"price": {"id": "price_monthly"}, "current_period_end": 1_790_000_000}]},
                },
            ),
        )

        row = BillingCustomer.objects.get(stripe_customer_id="cus_known")
        assert row.status == "active"
        assert row.price_id == "price_monthly"
        assert row.current_period_end is not None
        assert is_paid(tenancy.organization) is True

    def test_deletion_cancels_and_clears_the_subscription_id(self, client: Any, tenancy: Any) -> None:
        from apps.billing.entitlements import is_paid

        customer_row(tenancy, status="active", stripe_subscription_id="sub_1")

        post(client, event("customer.subscription.deleted", {"id": "sub_1", "customer": "cus_known"}))

        row = BillingCustomer.objects.get(stripe_customer_id="cus_known")
        assert row.status == "canceled"
        assert row.stripe_subscription_id == ""
        assert is_paid(tenancy.organization) is False
