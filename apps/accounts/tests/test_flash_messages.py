"""What allauth is allowed to say.

allauth narrates: it flashes "Confirmation email sent to you" and "Successfully
signed in as you" onto the first page a new account ever sees, above a sidebar
already showing that address. ``AccountAdapter.add_message`` drops that, and
keeps anything allauth raises at ERROR — which is the only way a user learns
that something did not work.
"""

import pytest
from allauth.account.adapter import get_adapter
from django.contrib import messages
from django.contrib.messages import get_messages
from django.contrib.messages.middleware import MessageMiddleware
from django.contrib.sessions.middleware import SessionMiddleware
from django.http import HttpResponse
from django.test import RequestFactory

from apps.accounts.adapters import AccountAdapter


def _request():
    """A request with the session and message storage allauth writes into."""
    request = RequestFactory().get("/")
    SessionMiddleware(lambda r: HttpResponse())(request)
    MessageMiddleware(lambda r: HttpResponse())(request)
    return request


@pytest.mark.django_db
class TestAllauthNarrationIsDropped:
    def test_signing_up_lands_on_a_page_with_no_notices(self, client):
        """The whole point: the two notices this change exists to remove are
        added during signup, and base.html renders every message in one strip
        above the page's own content."""
        response = client.post(
            "/accounts/signup/",
            {"email": "someone@example.test", "password1": "a-long-enough-passphrase"},
            follow=True,
        )

        assert response.status_code == 200
        body = response.content.decode()
        assert "alert-info" not in body
        assert "alert-success" not in body

    def test_the_adapter_is_the_one_allauth_actually_uses(self):
        """Asserted rather than assumed: the filter is a setting away from being
        inert, and ACCOUNT_ADAPTER is the only thing wiring it in."""
        assert isinstance(get_adapter(), AccountAdapter)

    @pytest.mark.parametrize(
        "template",
        [
            "account/messages/logged_in.txt",
            "account/messages/logged_out.txt",
            "account/messages/email_confirmation_sent.txt",
            "socialaccount/messages/account_connected.txt",
        ],
    )
    def test_nothing_below_error_reaches_the_user(self, template):
        """socialaccount's flows call get_account_adapter() too, so its notices
        arrive here as well — which is why this is one override and not
        eighteen empty message templates."""
        request = _request()

        AccountAdapter().add_message(request, messages.SUCCESS, template)
        AccountAdapter().add_message(request, messages.INFO, template)

        assert list(get_messages(request)) == []

    @pytest.mark.parametrize(
        "template",
        [
            "account/messages/cannot_delete_primary_email.txt",
            "account/messages/unverified_primary_email.txt",
            "account/messages/email_confirmation_failed.txt",
            "socialaccount/messages/account_connected_other.txt",
        ],
    )
    def test_a_failure_still_reaches_the_user(self, template):
        """These are reachable — allauth.urls mounts /accounts/email/ and the
        Google provider — and a user who cannot delete their primary address has
        no other way to find out why.

        Asserted as "something was delivered, addressed to this user" rather
        than as allauth's exact sentence: the wording is upstream's, rendered
        through its own i18n templates, so pinning the English would fail this
        on an allauth release that rephrases it or a deployment whose
        LANGUAGE_CODE is not en — neither of which says anything about the
        filter under test.
        """
        request = _request()

        AccountAdapter().add_message(
            request,
            messages.ERROR,
            template,
            {"email": "someone@example.test", "sociallogin": None, "action": None},
        )

        delivered = list(get_messages(request))
        assert [m.level for m in delivered] == [messages.ERROR]
        assert str(delivered[0]).strip()


@pytest.mark.django_db
class TestTheAppsOwnMessagesAreUntouched:
    def test_a_view_can_still_report_a_failure(self, tenancy, client_for):
        """The filter is on allauth's adapter, not on django.contrib.messages —
        the app's own feedback goes through the latter and must survive."""
        response = client_for(tenancy.user_for("admin")).post(
            f"/w/{tenancy.workspace.pk}/settings/update/", {"name": "  "}, follow=True
        )

        assert "alert-error" in response.content.decode()
