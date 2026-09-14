"""What Stripe actually receives.

Every assertion here is against the decoded form body of a real request built by
the real SDK, not against the Python arguments handed to it. That distinction is
the whole reason the fake sits at the transport rather than at the call: Stripe's
bracket-notation encoding is where a nested parameter gets silently dropped, and
a test that checked the arguments would pass through that.

The single most important case in the file is
:meth:`TestCheckoutSession.test_the_price_is_the_configured_one`. "Never trust
client input for which plan" is a security property, and this is the only place
it is observable.
"""

from typing import Any

import pytest

from apps.billing import stripe_client
from apps.billing.tests.stripe_support import fake_stripe

ORG_ID = "0192f3a4-5b6c-7d8e-9f01-234567890abc"


class TestHeaders:
    """The headers every call carries, whatever the call is."""

    def test_the_api_version_is_pinned(self) -> None:
        """Unpinned, a Stripe dashboard change alters response shapes under a
        running deployment with no deployment of ours involved."""
        with fake_stripe() as fake:
            stripe_client.create_customer(organization_id=ORG_ID, email="a@example.test", name="Acme")

        assert fake.calls[0].headers["Stripe-Version"] == stripe_client.STRIPE_API_VERSION

    def test_the_key_travels_as_a_bearer_token(self) -> None:
        with fake_stripe() as fake:
            stripe_client.create_customer(organization_id=ORG_ID, email="a@example.test", name="Acme")

        assert fake.calls[0].headers["Authorization"].startswith("Bearer ")


class TestCustomer:
    def test_it_posts_to_the_customers_endpoint(self) -> None:
        with fake_stripe() as fake:
            stripe_client.create_customer(organization_id=ORG_ID, email="ada@example.test", name="Acme")

        call = fake.calls[0]
        assert (call.method, call.path) == ("POST", "/v1/customers")
        assert call.form["email"] == "ada@example.test"
        assert call.form["metadata[organization_id]"] == ORG_ID

    def test_the_idempotency_key_is_deterministic(self) -> None:
        """Two identical calls carry the same key, so a double-submitted form
        cannot mint two customers for one organization."""
        with fake_stripe() as fake:
            stripe_client.create_customer(organization_id=ORG_ID, email="a@example.test", name="Acme")
            stripe_client.create_customer(organization_id=ORG_ID, email="a@example.test", name="Acme")

        keys = {call.headers["Idempotency-Key"] for call in fake.calls}
        assert keys == {f"customer:{ORG_ID}"}


class TestCheckoutSession:
    def _create(self, fake_price: str = "price_configured_monthly") -> Any:
        return stripe_client.create_checkout_session(
            customer_id="cus_123",
            price_id=fake_price,
            success_url="https://app.test/organization/billing/?checkout=success",
            cancel_url="https://app.test/organization/billing/?checkout=cancelled",
            organization_id=ORG_ID,
        )

    def test_the_price_is_the_configured_one(self) -> None:
        """The security property, as an observation of the wire.

        Callers map a "monthly"/"yearly" choice onto a settings-held price id and
        never pass one through from a request. This asserts the nested line item
        survives encoding, which is the half a call-level stub cannot see.
        """
        with fake_stripe() as fake:
            self._create()

        form = fake.calls_to("POST", "/v1/checkout/sessions")[0].form
        assert form["line_items[0][price]"] == "price_configured_monthly"
        assert form["line_items[0][quantity]"] == "1"
        assert form["mode"] == "subscription"

    def test_the_organization_travels_on_the_subscription_too(self) -> None:
        """Not redundant with the session metadata: ``subscription_data`` is what
        puts the org on every later ``customer.subscription.updated``, which the
        session's own metadata never reaches."""
        with fake_stripe() as fake:
            self._create()

        form = fake.calls_to("POST", "/v1/checkout/sessions")[0].form
        assert form["client_reference_id"] == ORG_ID
        assert form["metadata[organization_id]"] == ORG_ID
        assert form["subscription_data[metadata][organization_id]"] == ORG_ID

    def test_the_return_urls_are_sent_verbatim(self) -> None:
        with fake_stripe() as fake:
            self._create()

        form = fake.calls_to("POST", "/v1/checkout/sessions")[0].form
        assert form["success_url"] == "https://app.test/organization/billing/?checkout=success"
        assert form["cancel_url"] == "https://app.test/organization/billing/?checkout=cancelled"

    def test_each_session_gets_a_fresh_idempotency_key(self) -> None:
        """Somebody who abandons checkout and comes back must get a new session;
        a deterministic key would hand them the expired one."""
        with fake_stripe() as fake:
            self._create()
            self._create()

        keys = {call.headers["Idempotency-Key"] for call in fake.calls}
        assert len(keys) == 2


class TestPortalSession:
    def test_the_configuration_is_forwarded_when_set(self) -> None:
        with fake_stripe() as fake:
            stripe_client.create_portal_session(
                customer_id="cus_123", return_url="https://app.test/organization/billing/", configuration_id="bpc_42"
            )

        assert fake.calls[0].form["configuration"] == "bpc_42"

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_a_blank_configuration_is_omitted_not_sent_empty(self, blank: str) -> None:
        """Stripe answers ``configuration=`` with a 400; omitting it correctly
        falls back to the account default. The same empty-versus-absent
        distinction config/settings/base.py warns about, arriving from the other
        end — and a pasted-whitespace value has to read as blank too."""
        with fake_stripe() as fake:
            stripe_client.create_portal_session(
                customer_id="cus_123", return_url="https://app.test/organization/billing/", configuration_id=blank
            )

        assert "configuration" not in fake.calls[0].form


class TestFailures:
    """Stripe's own error mapping runs, and one exception comes out."""

    @pytest.mark.parametrize(("status", "label"), [(429, "rate limited"), (500, "server error"), (402, "card error")])
    def test_every_refusal_becomes_one_exception(self, status: int, label: str) -> None:
        with (
            fake_stripe(
                lambda fake: fake.reply(
                    "POST", "/v1/customers", {"error": {"type": "api_error", "message": label}}, status=status
                )
            ),
            pytest.raises(stripe_client.StripeUnavailableError),
        ):
            stripe_client.create_customer(organization_id=ORG_ID, email="a@example.test", name="Acme")

    def test_stripes_own_message_is_not_carried_on_the_exception(self) -> None:
        """A provider's error text routinely quotes the request that produced it,
        and a caller renders this. It names the operation and nothing else."""
        secret_ish = "sk_live_should_never_be_echoed"
        with (
            fake_stripe(
                lambda fake: fake.reply(
                    "POST", "/v1/customers", {"error": {"type": "api_error", "message": secret_ish}}, status=400
                )
            ),
            pytest.raises(stripe_client.StripeUnavailableError) as caught,
        ):
            stripe_client.create_customer(organization_id=ORG_ID, email="a@example.test", name="Acme")

        assert secret_ish not in str(caught.value)
        assert str(caught.value) == "customers.create"


def test_the_seam_is_restored_afterwards() -> None:
    """The context manager must put ``_client`` back, or one test's fake leaks
    into every test after it in the same process."""
    before = stripe_client._client
    with fake_stripe():
        assert stripe_client._client is not before
    assert stripe_client._client is before
