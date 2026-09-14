"""With Stripe unconfigured, every organization is unlimited.

This is the promise the whole billing feature rests on, and the reason it was
allowed to exist in a tree whose specification said "there is exactly one tier".
BrightBean Chat is AGPL and self-hostable; a self-hoster is not running a
generous free tier, they are running the entire product, and nothing in
``apps.billing`` may change that.

The file is named so it cannot be deleted quietly.

Two properties, and the second is the one that catches real mistakes:

*Unit* — ``is_paid`` is True, ``limits_for`` is ``UNLIMITED``, and **every**
``check_*`` in the module passes. Every, not a list somebody maintained: the
checks are discovered by introspection, so a sixth limit added next year is
covered the day it lands rather than the day somebody remembers this file. That
is the discipline ``tests/acceptance/test_send_boundary.py`` uses for its own
deny-by-default scan.

*Behavioural* — driving the real surfaces past every free cap and asserting
nothing refuses. That half lands with the gates themselves, because until a call
site is wired to a check there is no code path for it to exercise and a green
result would mean nothing. It is named here so the gap is visible rather than
forgotten.

The most valuable single case in the file is
``test_a_cancelled_row_still_reads_as_unlimited``. A naive implementation reads
the row before the switch and gets it wrong, and the state it gets wrong is a
real one: a deployment that billed for a while and then turned Stripe off would
strand its customers on a plan they never chose.
"""

import inspect
from typing import Any

import pytest

from apps.billing import entitlements
from apps.billing.models import ActiveContactMonth, BillingCustomer
from apps.billing.plans import UNLIMITED, PlanKey
from apps.billing.tests.stripe_support import SECRET_KEY

pytestmark = pytest.mark.django_db

#: Every guard in the module, found rather than listed. All five take the
#: organization and nothing else; a future check with a different signature
#: would fail here loudly, which is the correct outcome — it needs its own case.
CHECKS = sorted(
    name for name, value in vars(entitlements).items() if name.startswith("check_") and inspect.isfunction(value)
)


def test_the_discovery_actually_found_the_guards() -> None:
    """A scan that silently finds nothing passes every test built on it."""
    assert len(CHECKS) >= 5, f"expected the guards to be discovered, got {CHECKS}"


class TestStripeUnconfigured:
    """The supported state for every self-hosted install.

    pytest-django's ``settings`` fixture rather than ``override_settings`` as a
    class decorator: that decorator only accepts ``SimpleTestCase`` subclasses,
    and every test class in this project is a plain one.
    """

    @pytest.fixture(autouse=True)
    def _stripe_off(self, settings: Any) -> None:
        settings.STRIPE_ENABLED = False

    def test_billing_is_reported_as_disabled(self) -> None:
        assert entitlements.billing_enabled() is False

    def test_every_organization_is_paid(self, tenancy: Any) -> None:
        assert entitlements.is_paid(tenancy.organization) is True
        assert entitlements.plan_key(tenancy.organization) == PlanKey.PAID

    def test_limits_are_unlimited(self, tenancy: Any) -> None:
        assert entitlements.limits_for(tenancy.organization) is UNLIMITED

    def test_a_cancelled_row_still_reads_as_unlimited(self, tenancy: Any) -> None:
        """The row exists and says canceled; the switch is off, so it is not read.

        This is the case a naive `is_paid` gets wrong — reading the row first and
        the switch second — and it is not hypothetical: it is the state of any
        deployment that billed for a while and then stopped.
        """
        BillingCustomer.objects.create(
            organization=tenancy.organization,
            stripe_customer_id="cus_cancelled",
            status="canceled",
        )

        assert entitlements.is_paid(tenancy.organization) is True
        assert entitlements.limits_for(tenancy.organization) is UNLIMITED

    @pytest.mark.parametrize("check_name", CHECKS)
    def test_no_guard_refuses_anything(self, tenancy: Any, check_name: str) -> None:
        """Every guard, including ones that do not exist yet."""
        getattr(entitlements, check_name)(tenancy.organization)

    def test_nothing_is_metered(self, tenancy: Any, django_assert_num_queries: Any) -> None:
        """A self-hosted install writes no meter rows at all.

        Both a performance property and a "we are not instrumenting you" one: an
        unlimited plan has no number to keep, so the table stays empty and the
        send path pays nothing for it.
        """
        from apps.billing.metering import meter

        contact = _contact(tenancy.workspace)

        from apps.billing.metering import MarkOutcome

        assert meter(tenancy.organization, contact) is MarkOutcome.NOT_METERED
        assert ActiveContactMonth.objects.unscoped().count() == 0


class TestStripeConfigured:
    """The mirror image, so the tests above cannot be passing vacuously.

    Without this, deleting the early return in ``limits_for`` would leave the
    whole class above green — every assertion there is satisfied by a function
    that returns UNLIMITED unconditionally.
    """

    @pytest.fixture(autouse=True)
    def _stripe_on(self, settings: Any) -> None:
        settings.STRIPE_ENABLED = True
        settings.STRIPE_SECRET_KEY = SECRET_KEY
        settings.STRIPE_PRICE_ID_MONTHLY = "price_monthly"
        settings.STRIPE_PRICE_ID_YEARLY = "price_yearly"

    def test_an_organization_with_no_row_is_free(self, tenancy: Any) -> None:
        assert entitlements.is_paid(tenancy.organization) is False
        assert entitlements.limits_for(tenancy.organization).seats == 1

    def test_an_active_subscription_is_paid(self, tenancy: Any) -> None:
        BillingCustomer.objects.create(
            organization=tenancy.organization,
            stripe_customer_id="cus_live",
            status="active",
        )

        assert entitlements.is_paid(tenancy.organization) is True

    @pytest.mark.parametrize("status", ["active", "trialing", "past_due"])
    def test_the_statuses_that_entitle(self, tenancy: Any, status: str) -> None:
        """``past_due`` entitles: Stripe retries a bounced card for days, and the
        Customer Portal is the only place the customer can fix it."""
        BillingCustomer.objects.create(
            organization=tenancy.organization, stripe_customer_id=f"cus_{status}", status=status
        )

        assert entitlements.is_paid(tenancy.organization) is True

    @pytest.mark.parametrize(
        "status", ["canceled", "unpaid", "incomplete", "incomplete_expired", "paused", "something_stripe_invented"]
    )
    def test_the_statuses_that_do_not(self, tenancy: Any, status: str) -> None:
        """Including one nobody has heard of. A status the code does not
        recognise is not a status to guess permissively about."""
        BillingCustomer.objects.create(
            organization=tenancy.organization, stripe_customer_id=f"cus_{status}", status=status
        )

        assert entitlements.is_paid(tenancy.organization) is False

    def test_the_free_seat_limit_refuses_a_second_seat(self, tenancy: Any) -> None:
        """Proves the guards can actually say no — the other half of the
        parametrised pass above."""
        from apps.billing.entitlements import PlanLimitError

        # The tenancy fixture already seats more than one member.
        with pytest.raises(PlanLimitError) as caught:
            entitlements.check_can_add_seat(tenancy.organization)

        assert caught.value.code == "plan_seats"


def _contact(workspace: Any) -> Any:
    from apps.contacts.models import Contact

    return Contact.objects.create(workspace=workspace, first_name="Ada")
