"""What the billing page shows, in each of the four states it has.

The state that matters most is the first one below. On a deployment with no
payment provider the page still renders — the usage figures are useful on
anybody's box — but it must **not** render a Free-versus-Pro comparison, because
on that box the Free column's limits are fiction. Showing them would be the one
thing SPEC §1.1's promise actually forbids: telling a self-hoster they are on a
restricted plan when they are not.
"""

from datetime import timedelta
from typing import Any

import pytest
from django.utils import timezone

from apps.billing.models import BillingCustomer
from apps.billing.tests.stripe_support import SECRET_KEY
from tests.markup import nav_icon_names

pytestmark = pytest.mark.django_db

URL = "/organization/billing/"


@pytest.fixture
def stripe_on(settings: Any) -> None:
    settings.STRIPE_ENABLED = True
    settings.STRIPE_SECRET_KEY = SECRET_KEY
    settings.STRIPE_PRICE_ID_MONTHLY = "price_m"
    settings.STRIPE_PRICE_ID_YEARLY = "price_y"


def paid(tenancy: Any, **kwargs: Any) -> BillingCustomer:
    return BillingCustomer.objects.create(
        organization=tenancy.organization,
        stripe_customer_id="cus_live",
        status=kwargs.pop("status", "active"),
        **kwargs,
    )


def body(response: Any) -> str:
    return response.content.decode()


class TestSelfHosted:
    """No Stripe configured. Every self-hosted install is here."""

    def test_the_page_still_renders(self, client_for: Any, tenancy: Any, settings: Any) -> None:
        settings.STRIPE_ENABLED = False

        response = client_for(tenancy.owner).get(URL)

        assert response.status_code == 200

    def test_it_shows_no_plan_comparison_and_no_subscribe_button(
        self, client_for: Any, tenancy: Any, settings: Any
    ) -> None:
        settings.STRIPE_ENABLED = False

        text = body(client_for(tenancy.owner).get(URL))

        assert ">Subscribe<" not in text
        assert "Current plan" not in text
        assert "Self-hosted" in text

    def test_it_still_shows_usage(self, client_for: Any, tenancy: Any, settings: Any) -> None:
        """The figures are read from the same models everything else reads, and
        "how many people did we talk to this month" is worth answering whether
        or not anybody is selling anything."""
        settings.STRIPE_ENABLED = False

        text = body(client_for(tenancy.owner).get(URL))

        assert "Contacts reached this month" in text
        assert "Channels connected" in text

    def test_no_usage_figure_claims_a_limit(self, client_for: Any, tenancy: Any, settings: Any) -> None:
        """ "3 of 25" on a box with no limits would be a lie told in a number."""
        settings.STRIPE_ENABLED = False

        text = body(client_for(tenancy.owner).get(URL))

        assert " of 25" not in text
        assert " of 2" not in text


@pytest.mark.usefixtures("stripe_on")
class TestOnTheFreePlan:
    def test_free_is_marked_as_the_current_plan(self, client_for: Any, tenancy: Any) -> None:
        text = body(client_for(tenancy.owner).get(URL))

        assert "Current plan" in text
        assert text.index("Free") < text.index("Current plan")

    def test_there_is_one_subscribe_button(self, client_for: Any, tenancy: Any) -> None:
        """One call to action, not one per interval."""
        text = body(client_for(tenancy.owner).get(URL))

        assert text.count(">Subscribe<") == 1
        assert 'action="/organization/billing/checkout/"' in text

    def test_both_intervals_are_still_reachable(self, client_for: Any, tenancy: Any) -> None:
        """The interval moved into a segmented control rather than being
        dropped — a yearly price nobody can select is a price that does not
        exist."""
        text = body(client_for(tenancy.owner).get(URL))

        assert 'value="monthly"' in text
        assert 'value="yearly"' in text

    def test_monthly_is_preselected(self, client_for: Any, tenancy: Any) -> None:
        """Posting the form untouched has to carry an interval, or the view
        refuses it against its own allowlist."""
        text = body(client_for(tenancy.owner).get(URL))
        monthly = text.index('value="monthly"')

        assert "checked" in text[monthly : monthly + 60]

    def test_manage_billing_is_not_offered(self, client_for: Any, tenancy: Any) -> None:
        assert "Manage billing" not in body(client_for(tenancy.owner).get(URL))

    def test_the_limits_are_shown_against_the_usage(self, client_for: Any, tenancy: Any) -> None:
        text = body(client_for(tenancy.owner).get(URL))

        assert "of 25" in text


@pytest.mark.usefixtures("stripe_on")
class TestOnThePaidPlan:
    def test_manage_billing_is_offered_and_subscribe_is_not(self, client_for: Any, tenancy: Any) -> None:
        paid(tenancy)

        text = body(client_for(tenancy.owner).get(URL))

        assert "Manage billing" in text
        assert 'action="/organization/billing/portal/"' in text
        assert ">Subscribe<" not in text

    def test_the_renewal_date_is_shown(self, client_for: Any, tenancy: Any) -> None:
        paid(tenancy, current_period_end=timezone.now() + timedelta(days=20))

        assert "Renews on" in body(client_for(tenancy.owner).get(URL))

    def test_a_cancelling_plan_says_so_and_says_nothing_is_deleted(self, client_for: Any, tenancy: Any) -> None:
        """The sentence a reader most needs at that moment."""
        paid(tenancy, cancel_at_period_end=True, current_period_end=timezone.now() + timedelta(days=8))

        text = body(client_for(tenancy.owner).get(URL))

        assert "Your plan ends on" in text
        assert "Nothing is deleted" in text


@pytest.mark.usefixtures("stripe_on")
class TestComingBackFromCheckout:
    def test_a_cancelled_checkout_says_no_charge_was_made(self, client_for: Any, tenancy: Any) -> None:
        text = body(client_for(tenancy.owner).get(URL + "?checkout=cancelled"))

        assert "No charge was made" in text

    def test_a_pending_checkout_says_it_is_activating(self, client_for: Any, tenancy: Any) -> None:
        """The browser can beat the webhook back."""
        BillingCustomer.objects.create(
            organization=tenancy.organization,
            stripe_customer_id="cus_pending",
            checkout_pending_since=timezone.now(),
        )

        assert "being activated" in body(client_for(tenancy.owner).get(URL + "?checkout=success"))

    def test_returning_with_a_success_flag_alone_grants_nothing(self, client_for: Any, tenancy: Any) -> None:
        """The URL is not evidence. A reader can type it; only the webhook, or
        the reconcile job, can say somebody paid."""
        text = body(client_for(tenancy.owner).get(URL + "?checkout=success"))

        assert "Manage billing" not in text
        assert ">Subscribe<" in text

    def test_a_stale_pending_checkout_stops_claiming_anything(self, client_for: Any, tenancy: Any) -> None:
        """Past the point a webhook was ever likely, the reconcile job owns it
        and the page stops saying something is in flight."""
        BillingCustomer.objects.create(
            organization=tenancy.organization,
            stripe_customer_id="cus_stale",
            checkout_pending_since=timezone.now() - timedelta(hours=3),
        )

        assert "being activated" not in body(client_for(tenancy.owner).get(URL))


@pytest.mark.usefixtures("stripe_on")
class TestWhoCanSeeWhat:
    def test_an_ordinary_member_sees_the_page(self, client_for: Any, tenancy: Any) -> None:
        """Everybody can see which plan they are on and how much is used."""
        response = client_for(tenancy.user_for("agent")).get(URL)

        assert response.status_code == 200
        assert "Contacts reached this month" in body(response)

    def test_an_ordinary_member_gets_no_controls(self, client_for: Any, tenancy: Any) -> None:
        text = body(client_for(tenancy.user_for("agent")).get(URL))

        assert ">Subscribe<" not in text
        assert "Only organization admins" in text

    def test_anonymous_is_sent_to_login(self, client: Any) -> None:
        assert client.get(URL).status_code == 302


@pytest.mark.usefixtures("stripe_on")
class TestTheUsageColouring:
    """Amber near a cap, red at it — but only for a cap you can spend down.

    Found by looking at the rendered page rather than by a test: the free plan
    has one user and one workspace *by definition*, so flagging those painted
    two rows red on a brand new account that had done nothing wrong, and
    permanently. A colour that is always on teaches the reader to ignore it
    everywhere else, including on the row that matters.
    """

    def test_a_cap_of_one_that_is_met_is_not_flagged(self, tenancy: Any) -> None:
        from apps.billing.selectors import billing_context

        rows = {row["label"]: row for row in billing_context(tenancy.organization)["usage_rows"]}

        assert rows["Workspaces"]["used"] == 1
        assert rows["Workspaces"]["limit"] == 1
        assert rows["Workspaces"]["at_limit"] is False
        assert rows["Workspaces"]["near_limit"] is False

    def test_a_spendable_cap_that_is_met_is_flagged(self, tenancy: Any) -> None:
        from apps.billing.selectors import billing_context
        from apps.channels.models import ChannelConnection
        from apps.common.platforms import Platform

        for index in range(2):
            ChannelConnection(
                workspace=tenancy.workspace,
                platform=Platform.TELEGRAM.value,
                display_name=f"bot {index}",
                external_id=f"colour-{tenancy.workspace.pk}-{index}",
            ).save()

        rows = {row["label"]: row for row in billing_context(tenancy.organization)["usage_rows"]}

        assert rows["Channels connected"]["at_limit"] is True

    def test_an_unlimited_row_is_never_flagged(self, tenancy: Any) -> None:
        from apps.billing.selectors import billing_context

        paid(tenancy)
        rows = billing_context(tenancy.organization)["usage_rows"]

        assert all(row["at_limit"] is False and row["near_limit"] is False for row in rows)


@pytest.mark.usefixtures("stripe_on")
class TestTheIcons:
    """Every glyph the page asks for is one the partial actually draws.

    ``templates/partials/_nav_icon.html`` renders a neutral dot for a name it
    does not recognise — deliberately, so a mistyped nav key is visible rather
    than collapsing a row. On this page that same kindness is a trap: five
    usage rows quietly drawing dots looks like a design choice, not a bug, and
    nothing else would ever fail.
    """

    def test_every_usage_row_names_a_glyph_the_partial_draws(self, tenancy: Any) -> None:
        from apps.billing.selectors import billing_context

        known = nav_icon_names()
        assert known, "the icon partial was parsed but yielded no names"

        for row in billing_context(tenancy.organization)["usage_rows"]:
            assert row["icon"] in known, f"{row['label']!r} asks for a glyph the partial does not draw"

    def test_every_plan_feature_names_a_glyph_the_partial_draws(self) -> None:
        """Same trap, on the column that is being sold: eight features quietly
        drawing identical dots looks deliberate."""
        from apps.billing.plans import PLAN_COPY

        known = nav_icon_names()

        for card in PLAN_COPY:
            for feature in card.features:
                assert feature.icon in known, f"{card.name}/{feature.text!r} asks for a glyph that is not drawn"

    def test_only_the_paid_column_carries_the_logo(self) -> None:
        """The mark is what makes one column read as the thing on offer. On both
        it would mean nothing."""
        from apps.billing.plans import PLAN_COPY

        assert [card.key for card in PLAN_COPY if card.show_logo] == ["paid"]

    def test_the_rendered_page_draws_one_glyph_per_usage_row(self, client_for: Any, tenancy: Any) -> None:
        """The end of the chain: names resolve, and the SVGs reach the HTML."""
        text = body(client_for(tenancy.owner).get(URL))

        assert text.count('class="plan-usage-icon"') == 5
        # Six Free features plus seven Pro ones, each with a check.
        assert text.count("plan-card-features") >= 2


@pytest.mark.usefixtures("stripe_on")
class TestThePricing:
    """What the page quotes, and whether the discount it claims is true."""

    def test_both_prices_are_in_the_html(self, client_for: Any, tenancy: Any) -> None:
        """Both are rendered and the toggle swaps them in CSS, so the switch
        costs no request and no script — which is what keeps this page working
        under a CSP of 'self' with no inline handler."""
        text = body(client_for(tenancy.owner).get(URL))

        assert "$15" in text
        assert "$12" in text

    def test_monthly_is_the_one_shown_first(self, client_for: Any, tenancy: Any) -> None:
        text = body(client_for(tenancy.owner).get(URL))
        monthly = text.index('value="monthly"')

        assert "checked" in text[monthly : monthly + 60]

    def test_the_free_column_quotes_zero(self, client_for: Any, tenancy: Any) -> None:
        text = body(client_for(tenancy.owner).get(URL))

        assert "$0" in text
        assert "Free forever" in text

    def test_the_paid_plan_is_named_pro_chat(self, client_for: Any, tenancy: Any) -> None:
        assert "Pro Chat" in body(client_for(tenancy.owner).get(URL))

    def test_the_saving_claim_matches_the_two_prices(self) -> None:
        """The one number on this page that is *derived* rather than chosen.

        Somebody changing a price and leaving the badge alone would have the
        page advertising a discount it does not give — the kind of wrong that
        reaches a customer's card rather than a log. So the claim is checked
        against the arithmetic instead of being trusted.
        """
        from apps.billing.plans import PAID_PRICES, YEARLY_SAVING

        monthly = int(PAID_PRICES["monthly"].amount.lstrip("$"))
        yearly = int(PAID_PRICES["yearly"].amount.lstrip("$"))
        actual = round((1 - yearly / monthly) * 100)

        assert f"Save {actual}%" == YEARLY_SAVING, (
            f"the page claims {YEARLY_SAVING!r} but ${yearly}/mo against ${monthly}/mo is {actual}%"
        )

    def test_the_yearly_note_states_the_real_annual_total(self) -> None:
        """ "$12/month, billed yearly" is only honest if the total is 12x it."""
        from apps.billing.plans import PAID_PRICES

        yearly = int(PAID_PRICES["yearly"].amount.lstrip("$"))

        assert f"${yearly * 12}" in PAID_PRICES["yearly"].note

    def test_the_prices_are_display_copy_and_never_reach_stripe(self) -> None:
        """The amounts here are quoted to a reader; Stripe charges whatever the
        configured price id says. If a future change made one of these an input
        to checkout, the two could disagree silently — so the seam is pinned."""
        import inspect

        from apps.billing import services

        source = inspect.getsource(services)

        assert "PAID_PRICES" not in source
        assert "plans." not in source


class TestTheNavRow:
    def test_the_settings_nav_reaches_the_page(self, client_for: Any, tenancy: Any) -> None:
        """A page nobody can click to is not a feature."""
        text = body(client_for(tenancy.owner).get("/organization/settings/"))

        assert URL in text
        assert "Plan &amp; billing" in text or "Plan & billing" in text
