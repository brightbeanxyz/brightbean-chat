"""The sweep over ``tests/form_action.py``'s inventory.

Why any of this exists is in that module's docstring. This file holds the
rendered policy to the inventory from both ends, and the inventory to the code
that actually navigates to each provider.
"""

import copy
from typing import Any

import pytest

from apps.billing.services import STRIPE_HOST_SUFFIX
from apps.channels import instagram_oauth, messenger_oauth
from tests.form_action import (
    ANY_PAGE,
    DESTINATIONS,
    assert_page_permits_its_redirect,
    origin_of,
    permits,
    sources,
)


@pytest.mark.django_db
class TestThePolicyMatchesTheInventory:
    def test_it_is_self_plus_the_inventory_and_nothing_else(self, client) -> None:
        """Both directions in one assertion, off one request.

        Permitting everything in the table is what makes the flows work.
        Permitting nothing else is the half worth keeping a table for:
        ``form-action`` is what stops an injected form posting a page's contents
        to an origin of the attacker's choosing, every entry gives back a little
        of that, and an entry that buys nothing is pure loss.

        Sorted, because source order inside a directive carries no meaning and a
        test that fails on a reordering sends the next reader hunting for a
        security regression that is not there.
        """
        assert sorted(sources(client)) == sorted(["'self'", *(d.source for d in DESTINATIONS)])


@pytest.mark.django_db
class TestGoogleSsoEndToEnd:
    """The one integration whose redirect is issued by someone else's view.

    Messenger and Instagram assert this in their own suites, where the fixtures
    are. Google has no such suite and needs no tenancy — the login page is
    anonymous — so it lives here, and it is the only place the Google half of
    the policy is checked against a real ``Location`` rather than against
    allauth's constant. The button posts to allauth's provider view while the
    form sits on our login page, so it is the login page's policy that governs.
    """

    @pytest.fixture
    def google_configured(self, settings: Any) -> None:
        """SOCIALACCOUNT_PROVIDERS is built from the env vars at import, so the
        credentials have to be put back into the dict, not just onto settings.
        Deep-copied because pytest-django restores the attribute, not the
        nested object every other test would then share."""
        providers = copy.deepcopy(settings.SOCIALACCOUNT_PROVIDERS)
        providers["google"]["APP"] = {"client_id": "test-client-id", "secret": "test-secret"}
        settings.SOCIALACCOUNT_PROVIDERS = providers
        settings.GOOGLE_AUTH_CLIENT_ID = "test-client-id"
        settings.GOOGLE_AUTH_CLIENT_SECRET = "test-secret"  # noqa: S105 - a fake credential

    def test_the_login_pages_policy_permits_where_allauth_sends_the_browser(
        self, client, google_configured: None
    ) -> None:
        location = assert_page_permits_its_redirect(
            client, ANY_PAGE, submits_to="/accounts/google/login/", process="login"
        )

        assert origin_of(location) == "https://accounts.google.com"

    def test_the_button_is_a_post_because_a_get_initiated_login_is_forgeable(self, client, settings: Any) -> None:
        """If this ever flips, the form-action exposure goes away — and so does
        the CSRF protection the setting is actually there for, so it should not.
        The entry for Google in the inventory rests on this being False."""
        assert settings.SOCIALACCOUNT_LOGIN_ON_GET is False


class TestTheInventoryMatchesTheCodeThatNavigatesThere:
    """The drift guard.

    Each source is a literal in ``config/settings/base.py``, because settings
    cannot import the app modules that own these URLs without a circular import.
    So the agreement between the two is asserted here instead: move
    ``LOGIN_ROOT`` to a new host and this goes red, rather than Messenger
    quietly breaking in Chrome again.
    """

    @pytest.fixture
    def allowed(self) -> list[str]:
        return [d.source for d in DESTINATIONS]

    def test_messenger_starts_somewhere_the_inventory_permits(self, allowed: list[str]) -> None:
        assert permits(allowed, messenger_oauth.LOGIN_ROOT)

    def test_instagram_starts_somewhere_the_inventory_permits(self, allowed: list[str]) -> None:
        assert permits(allowed, instagram_oauth.AUTHORIZE_URL)

    def test_google_starts_somewhere_the_inventory_permits(self, allowed: list[str]) -> None:
        """allauth owns this URL, so it can move under us on a dependency bump."""
        from allauth.socialaccount.providers.google.views import GoogleOAuth2Adapter

        assert permits(allowed, GoogleOAuth2Adapter.authorize_url)

    @pytest.mark.parametrize(
        "host",
        [
            "stripe.com",
            "checkout.stripe.com",
            "billing.stripe.com",
            # Not a host Stripe uses today. It is here because services.py takes
            # the registrable domain on the grounds that "Stripe has changed
            # them before", and the policy has to have made the same bet.
            "pay.stripe.com",
        ],
    )
    def test_every_stripe_url_the_validator_accepts_is_permitted(self, allowed: list[str], host: str) -> None:
        """The two allowlists are one decision, so they are tested as one.

        ``apps/billing/services._stripe_url`` is what decides whether a URL
        Stripe returned reaches a ``Location`` header. Anything it lets through
        and ``form-action`` refuses is a redirect the browser drops in silence,
        with no server-side error to find — the #115 failure, arriving through
        billing instead of a channel. So this asserts against the rule that
        function applies rather than against the two hostnames it happens to see.
        """
        assert host == "stripe.com" or host.endswith(STRIPE_HOST_SUFFIX), "not a host _stripe_url would accept"

        assert permits(allowed, f"https://{host}/c/pay/cs_test_123")

    @pytest.mark.parametrize("host", ["checkout.stripe.com.evil.test", "notstripe.com", "stripe.com.evil.test"])
    def test_the_hosts_the_validator_refuses_are_not_permitted_either(self, allowed: list[str], host: str) -> None:
        """The suffix trick ``test_checkout_flow.py`` already guards server-side,
        checked against the policy too: a wildcard source that accidentally
        matched these would hand an injected form somewhere to post to."""
        assert not permits(allowed, f"https://{host}/pay")


class TestTheMatcher:
    """``permits`` is the thing every assertion above leans on, so it is checked
    rather than assumed. A matcher that answered True too readily would make the
    whole sweep vacuous."""

    def test_an_exact_origin_matches_only_itself(self) -> None:
        assert permits(["https://accounts.google.com"], "https://accounts.google.com/o/oauth2/v2/auth")
        assert not permits(["https://accounts.google.com"], "https://evil.test/")
        assert not permits(["https://accounts.google.com"], "https://accounts.google.com.evil.test/")

    def test_a_wildcard_matches_subdomains_but_not_the_domain_itself(self) -> None:
        """CSP's rule, which is why the inventory carries both apexes explicitly."""
        assert permits(["https://*.stripe.com"], "https://checkout.stripe.com/x")
        assert not permits(["https://*.stripe.com"], "https://stripe.com/x")
        assert not permits(["https://*.stripe.com"], "https://notstripe.com/x")

    def test_the_scheme_has_to_match(self) -> None:
        assert not permits(["https://*.facebook.com"], "http://www.facebook.com/x")

    def test_keyword_sources_never_match_an_off_origin_url(self) -> None:
        assert not permits(["'self'", "'none'"], "https://www.facebook.com/x")

    def test_origin_of_drops_userinfo(self) -> None:
        """``csp_origin``'s reason for existing, relied on by the failure message."""
        assert origin_of("https://key:secret@www.facebook.com/dialog") == "https://www.facebook.com"
