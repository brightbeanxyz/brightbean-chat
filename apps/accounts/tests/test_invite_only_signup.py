"""``SIGNUP=invite``: accounts are created only to honour an invitation.

One hook decides it — ``AccountAdapter.is_open_for_signup`` — and allauth asks it
for the local form directly and for Google through the default social adapter.
The Google tests pin that delegation, which is allauth's, not ours.
"""

from types import SimpleNamespace
from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.test import Client, RequestFactory
from django.urls import reverse
from django.utils import timezone

from apps.accounts.adapters import SocialAccountAdapter
from apps.accounts.services import signup_is_open
from apps.members.models import OrgMembership
from apps.members.roles import OrgRole, WorkspaceRole
from apps.members.services import create_invitation
from apps.members.signals_keys import PENDING_INVITE_SESSION_KEY
from apps.organizations.models import Organization
from tests.support import TEST_PASSWORD

User = get_user_model()
FORM = {"email": "invitee@example.test", "password1": TEST_PASSWORD}


@pytest.fixture
def closed(settings: Any) -> None:
    settings.SIGNUP = "invite"


def invitation(tenancy: Any) -> Any:
    return create_invitation(
        org=tenancy.organization,
        email="invitee@example.test",
        org_role=OrgRole.MEMBER,
        workspace_assignments=[{"workspace_id": str(tenancy.workspace.pk), "role": WorkspaceRole.EDITOR.value}],
        invited_by=tenancy.owner,
    )


def request_with_session(token: str | None = None) -> Any:
    request: Any = RequestFactory().get("/")
    request.session = {PENDING_INVITE_SESSION_KEY: token} if token else {}
    return request


@pytest.mark.django_db
class TestTheLocalForm:
    def test_it_is_open_by_default(self, client):
        response = client.post(reverse("account_signup"), FORM, follow=True)

        assert User.objects.filter(email=FORM["email"]).exists()
        assert response.status_code == 200

    def test_it_is_closed_without_an_invitation(self, client, closed):
        for response in (client.get(reverse("account_signup")), client.post(reverse("account_signup"), FORM)):
            assert "account/signup_closed.html" in [t.name for t in response.templates]

        assert not User.objects.filter(email=FORM["email"]).exists()
        assert Organization.objects.count() == 0

    def test_an_invitation_opens_it_and_joins_the_inviting_organization(self, tenancy, client, closed):
        token = invitation(tenancy).raw_token
        client.get(f"/invite/{token}/")

        response = client.get(reverse("account_signup"))
        assert "account/signup_closed.html" not in [t.name for t in response.templates]
        client.post(reverse("account_signup"), FORM, follow=True)

        memberships = OrgMembership.objects.filter(user__email=FORM["email"])
        assert [m.organization_id for m in memberships] == [tenancy.organization.pk]
        assert Organization.objects.count() == 1

    def test_an_expired_invitation_does_not_open_it(self, tenancy, client, closed):
        invite = invitation(tenancy)
        client.get(f"/invite/{invite.raw_token}/")
        invite.expires_at = timezone.now() - timezone.timedelta(seconds=1)
        invite.save(update_fields=["expires_at"])

        response = client.post(reverse("account_signup"), FORM)

        assert "account/signup_closed.html" in [t.name for t in response.templates]
        assert not User.objects.filter(email=FORM["email"]).exists()

    def test_an_invitation_opens_only_one_signup(self, tenancy, client, closed):
        client.get(f"/invite/{invitation(tenancy).raw_token}/")
        client.post(reverse("account_signup"), FORM, follow=True)
        client.logout()

        # The token is spent: a second visitor reusing it gets nothing.
        second = client.post(reverse("account_signup"), {**FORM, "email": "other@example.test"})

        assert "account/signup_closed.html" in [t.name for t in second.templates]
        assert not User.objects.filter(email="other@example.test").exists()

    def test_a_link_shared_with_a_second_person_gives_them_no_account_or_organization(self, tenancy, closed):
        """Both visitors pass the read-only check; the first spends the invitation.
        The second must not end up with an account, nor an organization of their own."""
        token = invitation(tenancy).raw_token
        first, second = Client(), Client()
        for visitor in (first, second):
            visitor.get(f"/invite/{token}/")
        first.post(reverse("account_signup"), FORM, follow=True)

        response = second.post(reverse("account_signup"), FORM)

        assert "account/signup_closed.html" in [t.name for t in response.templates]
        assert User.objects.filter(email=FORM["email"]).count() == 1
        assert Organization.objects.count() == 1

    def test_the_address_must_be_the_invited_one(self, tenancy, client, closed):
        """The field is read-only only in the browser; the server holds the line."""
        client.get(f"/invite/{invitation(tenancy).raw_token}/")

        response = client.post(reverse("account_signup"), {**FORM, "email": "someone-else@example.test"})

        assert "account/signup_closed.html" in [t.name for t in response.templates]
        assert not User.objects.filter(email="someone-else@example.test").exists()
        assert Organization.objects.count() == 1
        # A refusal does not cost the visitor their invitation.
        assert PENDING_INVITE_SESSION_KEY in client.session


@pytest.mark.django_db
class TestALostRace:
    def test_a_uniqueness_clash_is_the_closed_page_not_a_500(self, tenancy):
        from allauth.core.exceptions import ImmediateHttpResponse
        from django.db import IntegrityError

        from apps.accounts.adapters import _create_user_and_accept_invitation

        def clash() -> Any:
            raise IntegrityError("duplicate key")

        request = request_with_session(invitation(tenancy).raw_token)

        with pytest.raises(ImmediateHttpResponse) as closed:
            _create_user_and_accept_invitation(request, clash)

        assert closed.value.response.status_code == 403


@pytest.mark.django_db
class TestThePredicate:
    def test_an_open_installation_never_queries(self, django_assert_num_queries):
        with django_assert_num_queries(0):
            assert signup_is_open(request_with_session("anything")) is True

    def test_a_closed_one_needs_a_live_invitation(self, tenancy, closed):
        assert signup_is_open(request_with_session()) is False
        assert signup_is_open(request_with_session("not-a-token")) is False
        assert signup_is_open(request_with_session(invitation(tenancy).raw_token)) is True


@pytest.mark.django_db
class TestGoogleAndSso:
    """A social login is invite-only too, through the path single sign-on already has."""

    @staticmethod
    def sociallogin(provider: str, email: str = "invitee@example.test") -> Any:
        return SimpleNamespace(
            account=SimpleNamespace(provider=provider),
            email_addresses=[SimpleNamespace(email=email, verified=True)],
        )

    def test_google_is_closed_without_an_invitation(self, closed):
        adapter = SocialAccountAdapter()

        assert adapter.is_open_for_signup(request_with_session(), self.sociallogin("google")) is False

    def test_google_is_open_for_the_invited_address(self, tenancy, closed):
        request = request_with_session(invitation(tenancy).raw_token)

        assert SocialAccountAdapter().is_open_for_signup(request, self.sociallogin("google")) is True

    def test_google_is_closed_for_another_address(self, tenancy, closed):
        request = request_with_session(invitation(tenancy).raw_token)

        other = self.sociallogin("google", "someone-else@example.test")
        assert SocialAccountAdapter().is_open_for_signup(request, other) is False

    def test_google_is_open_by_default(self):
        assert SocialAccountAdapter().is_open_for_signup(request_with_session(), self.sociallogin("google")) is True

    def test_open_sso_is_invite_only_too(self, closed, settings):
        settings.OIDC_SIGNUP = "open"

        assert SocialAccountAdapter().is_open_for_signup(request_with_session(), self.sociallogin("sso")) is False


class TestTheSetting:
    def test_an_unknown_value_stops_startup(self, monkeypatch):
        """Settings are evaluated at import, so run the module afresh."""
        import runpy

        from django.core.exceptions import ImproperlyConfigured

        monkeypatch.setenv("SIGNUP", "closed")
        with pytest.raises(ImproperlyConfigured, match="SIGNUP"):
            runpy.run_module("config.settings.base")
