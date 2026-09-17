"""Starting and managing a subscription, from the POST to the redirect.

The assertions that matter here are about what the user cannot do: pick their
own price, reach the endpoint without being an org admin, reach it at all on a
deployment with no Stripe, or be redirected anywhere but Stripe.
"""

from typing import Any

import pytest
from django.urls import reverse

from apps.billing.models import BillingCustomer
from apps.billing.tests.stripe_support import SECRET_KEY, fake_stripe
from tests.form_action import permits, sources_of

pytestmark = pytest.mark.django_db

CHECKOUT_URL = "/organization/billing/checkout/"
PORTAL_URL = "/organization/billing/portal/"
STRIPE_CHECKOUT = "https://checkout.stripe.com/c/pay/cs_test_123"
STRIPE_PORTAL = "https://billing.stripe.com/p/session/live_456"


@pytest.fixture(autouse=True)
def _configured(settings: Any) -> None:
    settings.STRIPE_ENABLED = True
    settings.STRIPE_SECRET_KEY = SECRET_KEY
    settings.STRIPE_PRICE_ID_MONTHLY = "price_configured_monthly"
    settings.STRIPE_PRICE_ID_YEARLY = "price_configured_yearly"
    settings.STRIPE_PORTAL_CONFIGURATION_ID = "bpc_custom"
    settings.APP_URL = "https://app.example.test"


def sessions(fake: Any) -> None:
    fake.reply("POST", "/v1/customers", {"id": "cus_new", "object": "customer"})
    fake.reply("POST", "/v1/checkout/sessions", {"id": "cs_1", "object": "checkout.session", "url": STRIPE_CHECKOUT})
    fake.reply(
        "POST", "/v1/billing_portal/sessions", {"id": "bps_1", "object": "billing_portal.session", "url": STRIPE_PORTAL}
    )


class TestSubscribing:
    def test_it_303s_to_stripe(self, client_for: Any, tenancy: Any) -> None:
        """303 rather than 302: after a POST it tells the browser to GET, which
        is what stops the back button offering to resubmit the checkout."""
        client = client_for(tenancy.owner)

        with fake_stripe(sessions):
            response = client.post(CHECKOUT_URL, {"interval": "monthly"})

        assert response.status_code == 303
        assert response["Location"] == STRIPE_CHECKOUT

    def test_the_billing_pages_own_policy_permits_both_redirects(self, client_for: Any, tenancy: Any) -> None:
        """Issue #115, arriving through billing rather than through a channel.

        The two forms live on the billing page, so it is *that* page's
        ``form-action`` a browser checks the 303 against — Chrome and Safari
        apply the directive to every hop of the navigation a form submission
        starts. This docstring's predecessor in ``apps/billing/views.py`` claimed
        a server-side redirect was exempt, which is how both Stripe flows came to
        be shipped blocked. ``tests/form_action.py`` has the reasoning.

        Both buttons, because Checkout and the Customer Portal answer with
        different Stripe subdomains and the policy has to cover both.
        """
        client = client_for(tenancy.owner)
        page = client.get(reverse("organizations:billing"))
        assert page.status_code == 200, "the policy has to be read off the page that carries the forms"
        allowed = sources_of(page)

        with fake_stripe(sessions):
            checkout = client.post(CHECKOUT_URL, {"interval": "monthly"})["Location"]
        # open_portal needs a Stripe customer to manage; checkout above created
        # the row, so this is the one field it is still missing.
        BillingCustomer.objects.update(stripe_customer_id="cus_new")
        with fake_stripe(sessions):
            portal = client.post(PORTAL_URL)["Location"]

        assert permits(allowed, checkout), f"{checkout} is not permitted by {' '.join(allowed)}"
        assert permits(allowed, portal), f"{portal} is not permitted by {' '.join(allowed)}"

    def test_the_price_cannot_be_chosen_by_the_caller(self, client_for: Any, tenancy: Any) -> None:
        """The security property of this flow.

        The form posts an interval; a price id posted alongside it is ignored,
        and what reaches Stripe is the configured id. Asserted against the
        decoded wire body, because that is the only place it is observable.
        """
        client = client_for(tenancy.owner)

        with fake_stripe(sessions) as fake:
            client.post(CHECKOUT_URL, {"interval": "monthly", "price": "price_one_cent", "quantity": "0"})

        form = fake.calls_to("POST", "/v1/checkout/sessions")[0].form
        assert form["line_items[0][price]"] == "price_configured_monthly"
        assert form["line_items[0][quantity]"] == "1"
        assert "price_one_cent" not in form.values()

    def test_the_yearly_interval_picks_the_yearly_price(self, client_for: Any, tenancy: Any) -> None:
        client = client_for(tenancy.owner)

        with fake_stripe(sessions) as fake:
            client.post(CHECKOUT_URL, {"interval": "yearly"})

        form = fake.calls_to("POST", "/v1/checkout/sessions")[0].form
        assert form["line_items[0][price]"] == "price_configured_yearly"

    @pytest.mark.parametrize("interval", ["", "weekly", "MONTHLY", "price_evil", "monthly\x00"])
    def test_an_unknown_interval_is_refused_without_calling_stripe(
        self, client_for: Any, tenancy: Any, interval: str
    ) -> None:
        client = client_for(tenancy.owner)

        with fake_stripe(sessions) as fake:
            response = client.post(CHECKOUT_URL, {"interval": interval})

        assert response.status_code == 302, "a refusal redirects back with a message"
        assert fake.calls_to("POST", "/v1/checkout/sessions") == []

    def test_surrounding_whitespace_is_tolerated(self, client_for: Any, tenancy: Any) -> None:
        """The view strips before matching, which is what every other POST
        handler in this project does (apps/workspaces/views.py spells out the
        trap of matching first and stripping second). The allowlist is still an
        exact match afterwards, so this widens nothing: "MONTHLY" above is still
        refused."""
        client = client_for(tenancy.owner)

        with fake_stripe(sessions) as fake:
            response = client.post(CHECKOUT_URL, {"interval": "  monthly  "})

        assert response.status_code == 303
        assert fake.calls_to("POST", "/v1/checkout/sessions")[0].form["line_items[0][price]"] == (
            "price_configured_monthly"
        )

    def test_the_success_url_carries_stripes_own_placeholder_literally(self, client_for: Any, tenancy: Any) -> None:
        """``{CHECKOUT_SESSION_ID}`` is Stripe's template, not Python's. An
        f-string or .format() would eat those braces and the return would carry
        no session id at all."""
        client = client_for(tenancy.owner)

        with fake_stripe(sessions) as fake:
            client.post(CHECKOUT_URL, {"interval": "monthly"})

        form = fake.calls_to("POST", "/v1/checkout/sessions")[0].form
        assert form["success_url"].endswith("?checkout=success&session_id={CHECKOUT_SESSION_ID}")
        assert form["success_url"].startswith("https://app.example.test/")
        assert form["cancel_url"].endswith("?checkout=cancelled")

    def test_two_attempts_create_one_stripe_customer(self, client_for: Any, tenancy: Any) -> None:
        """Somebody who abandons checkout and comes back must not get a second
        customer — one organization, one customer, one subscription to manage."""
        client = client_for(tenancy.owner)

        with fake_stripe(sessions) as fake:
            client.post(CHECKOUT_URL, {"interval": "monthly"})
            client.post(CHECKOUT_URL, {"interval": "monthly"})

        assert len(fake.calls_to("POST", "/v1/customers")) == 1
        assert BillingCustomer.objects.count() == 1

    def test_it_records_that_a_checkout_is_pending(self, client_for: Any, tenancy: Any) -> None:
        """What the page reads to say "being activated", and what the reconcile
        job looks for when a webhook never arrives."""
        client = client_for(tenancy.owner)

        with fake_stripe(sessions):
            client.post(CHECKOUT_URL, {"interval": "monthly"})

        assert BillingCustomer.objects.get().checkout_pending_since is not None

    def test_an_already_paid_organization_is_not_sent_through_checkout_again(
        self, client_for: Any, tenancy: Any
    ) -> None:
        BillingCustomer.objects.create(
            organization=tenancy.organization, stripe_customer_id="cus_live", status="active"
        )
        client = client_for(tenancy.owner)

        with fake_stripe(sessions) as fake:
            response = client.post(CHECKOUT_URL, {"interval": "monthly"})

        assert response.status_code == 302
        assert fake.calls_to("POST", "/v1/checkout/sessions") == []


class TestOnlyOneCheckoutAtATime:
    """Two completed sessions mean two subscriptions on one customer.

    Each session carries a random idempotency key — it has to, so somebody who
    abandons one and comes back gets a fresh session — so nothing else stops a
    double-submit, or two admins starting at once, from being charged twice.
    """

    def test_a_second_attempt_while_one_is_pending_is_refused(self, client_for: Any, tenancy: Any) -> None:
        client = client_for(tenancy.owner)

        with fake_stripe(sessions) as fake:
            first = client.post(CHECKOUT_URL, {"interval": "monthly"})
            second = client.post(CHECKOUT_URL, {"interval": "monthly"})

        assert first.status_code == 303
        assert second.status_code == 302, "the second attempt should redirect back with a message"
        assert len(fake.calls_to("POST", "/v1/checkout/sessions")) == 1

    def test_an_abandoned_checkout_stops_blocking_after_the_window(self, client_for: Any, tenancy: Any) -> None:
        """The flag must not lock an organization out for good. The bound is the
        same one the reconcile job uses to decide a checkout needs chasing."""
        from datetime import timedelta

        from django.utils import timezone

        from apps.billing.housekeeping import PENDING_CHECKOUT_MINUTES

        client = client_for(tenancy.owner)
        with fake_stripe(sessions):
            client.post(CHECKOUT_URL, {"interval": "monthly"})

        BillingCustomer.objects.filter(organization=tenancy.organization).update(
            checkout_pending_since=timezone.now() - timedelta(minutes=PENDING_CHECKOUT_MINUTES + 1)
        )

        with fake_stripe(sessions) as fake:
            again = client.post(CHECKOUT_URL, {"interval": "monthly"})

        assert again.status_code == 303
        assert len(fake.calls_to("POST", "/v1/checkout/sessions")) == 1


class TestRedirectSafety:
    def test_a_non_stripe_url_is_refused(self, client_for: Any, tenancy: Any) -> None:
        """Stripe returning somewhere else means the account or the SDK has been
        tampered with — exactly when a blind redirect costs most."""
        client = client_for(tenancy.owner)

        def evil(fake: Any) -> None:
            sessions(fake)
            fake.reply(
                "POST",
                "/v1/checkout/sessions",
                {"id": "cs_1", "object": "checkout.session", "url": "https://phish.example/pay"},
            )

        with fake_stripe(evil):
            response = client.post(CHECKOUT_URL, {"interval": "monthly"})

        assert response.status_code == 302
        assert "phish.example" not in response["Location"]

    def test_a_plain_http_stripe_url_is_refused(self, client_for: Any, tenancy: Any) -> None:
        client = client_for(tenancy.owner)

        def downgraded(fake: Any) -> None:
            sessions(fake)
            fake.reply(
                "POST",
                "/v1/checkout/sessions",
                {"id": "cs_1", "object": "checkout.session", "url": "http://checkout.stripe.com/c/pay/x"},
            )

        with fake_stripe(downgraded):
            response = client.post(CHECKOUT_URL, {"interval": "monthly"})

        assert response.status_code == 302

    def test_a_lookalike_host_is_refused(self, client_for: Any, tenancy: Any) -> None:
        """``notstripe.com`` and ``stripe.com.evil.test`` must both fail."""
        client = client_for(tenancy.owner)

        def lookalike(fake: Any) -> None:
            sessions(fake)
            fake.reply(
                "POST",
                "/v1/checkout/sessions",
                {"id": "cs_1", "object": "checkout.session", "url": "https://checkout.stripe.com.evil.test/pay"},
            )

        with fake_stripe(lookalike):
            response = client.post(CHECKOUT_URL, {"interval": "monthly"})

        assert response.status_code == 302


class TestPortal:
    def test_it_303s_to_the_portal_with_the_custom_configuration(self, client_for: Any, tenancy: Any) -> None:
        BillingCustomer.objects.create(
            organization=tenancy.organization, stripe_customer_id="cus_live", status="active"
        )
        client = client_for(tenancy.owner)

        with fake_stripe(sessions) as fake:
            response = client.post(PORTAL_URL)

        assert response.status_code == 303
        assert response["Location"] == STRIPE_PORTAL
        assert fake.calls_to("POST", "/v1/billing_portal/sessions")[0].form["configuration"] == "bpc_custom"

    def test_a_blank_configuration_is_omitted(self, client_for: Any, tenancy: Any, settings: Any) -> None:
        settings.STRIPE_PORTAL_CONFIGURATION_ID = ""
        BillingCustomer.objects.create(
            organization=tenancy.organization, stripe_customer_id="cus_live", status="active"
        )
        client = client_for(tenancy.owner)

        with fake_stripe(sessions) as fake:
            client.post(PORTAL_URL)

        assert "configuration" not in fake.calls_to("POST", "/v1/billing_portal/sessions")[0].form

    def test_an_organization_with_no_customer_gets_a_404(self, client_for: Any, tenancy: Any) -> None:
        """Nothing to manage answers exactly like a route that was never there."""
        client = client_for(tenancy.owner)

        with fake_stripe(sessions):
            response = client.post(PORTAL_URL)

        assert response.status_code == 404


class TestGating:
    @pytest.mark.parametrize("url", [CHECKOUT_URL, PORTAL_URL])
    def test_both_routes_404_when_stripe_is_unconfigured(
        self, client_for: Any, tenancy: Any, settings: Any, url: str
    ) -> None:
        """The AGPL promise at the routing layer: a self-hoster's install has no
        checkout endpoint to find."""
        settings.STRIPE_ENABLED = False
        client = client_for(tenancy.owner)

        assert client.post(url, {"interval": "monthly"}).status_code == 404

    @pytest.mark.parametrize("url", [CHECKOUT_URL, PORTAL_URL])
    def test_an_ordinary_member_is_refused(self, client_for: Any, tenancy: Any, url: str) -> None:
        """Org-tier, admin only — the apps/api/views_keys.py gate, not a
        PERMISSION_KEYS entry."""
        client = client_for(tenancy.user_for("admin"))

        assert client.post(url, {"interval": "monthly"}).status_code == 403

    @pytest.mark.parametrize("url", [CHECKOUT_URL, PORTAL_URL])
    def test_anonymous_is_sent_to_login(self, client: Any, url: str) -> None:
        response = client.post(url, {"interval": "monthly"})

        assert response.status_code == 302
        assert reverse("account_login") in response["Location"]

    @pytest.mark.parametrize("url", [CHECKOUT_URL, PORTAL_URL])
    def test_get_is_not_allowed(self, client_for: Any, tenancy: Any, url: str) -> None:
        client = client_for(tenancy.owner)

        assert client.get(url).status_code == 405
