"""Signing in through the OIDC provider: who may be created, and what they get.

End to end through allauth's own views. Only the three calls that leave the
process are replaced — the discovery document, the token exchange and the
userinfo fetch — so state, PKCE, the adapter and the ``user_signed_up`` receiver
all run for real.
"""

import copy
from types import SimpleNamespace
from typing import Any
from unittest import mock
from urllib.parse import parse_qs, urlparse

import pytest
from allauth.socialaccount.models import SocialAccount
from allauth.socialaccount.providers.oauth2.client import OAuth2Client
from allauth.socialaccount.providers.openid_connect.views import OpenIDConnectOAuth2Adapter
from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.test import Client
from django.urls import reverse

from apps.members.models import Invitation, OrgMembership
from apps.members.roles import OrgRole, WorkspaceRole
from apps.members.services import MembershipError, create_invitation
from apps.members.signals_keys import PENDING_INVITE_SESSION_KEY
from apps.organizations.models import Organization
from tests.support import create_user

ISSUER = "https://auth.example.test/auth/v1"
LOGIN = "/accounts/login/"
START = "/accounts/oidc/sso/login/"
CALLBACK = "/accounts/oidc/sso/login/callback/"
DISCOVERY = {
    "issuer": ISSUER,
    "authorization_endpoint": f"{ISSUER}/oidc/authorize",
    "token_endpoint": f"{ISSUER}/oidc/token",
    "userinfo_endpoint": f"{ISSUER}/oidc/userinfo",
    "jwks_uri": f"{ISSUER}/oidc/certs",
}


@pytest.fixture
def sso(settings: Any) -> None:
    """SOCIALACCOUNT_PROVIDERS is built from the env at import, so put the app
    into the dict. Deep-copied because pytest-django restores the attribute, not
    the nested object."""
    providers = copy.deepcopy(settings.SOCIALACCOUNT_PROVIDERS)
    providers["openid_connect"]["APPS"] = [
        {
            "provider_id": "sso",
            "name": "Zirek",
            "client_id": "bb",
            "secret": "s3cret",  # noqa: S105 - a fake credential
            "settings": {"server_url": ISSUER, "email_authentication": True},
        }
    ]
    settings.SOCIALACCOUNT_PROVIDERS = providers
    settings.OIDC_CONFIGURED = True


def sign_in(client: Client, **claims: Any) -> Any:
    """Run the whole authorization-code round trip as the user the provider describes."""
    userinfo = {"sub": "sub-1", "email": "joiner@example.test", "email_verified": True, "name": "Jo Iner", **claims}
    with (
        mock.patch.object(OpenIDConnectOAuth2Adapter, "openid_config", DISCOVERY),
        mock.patch.object(OAuth2Client, "get_access_token", return_value={"access_token": "at"}),
        mock.patch.object(OpenIDConnectOAuth2Adapter, "_fetch_user_info", return_value=userinfo),
    ):
        started = client.post(START)
        assert started.status_code == 302
        query = parse_qs(urlparse(started["Location"]).query)
        assert query["code_challenge_method"] == ["S256"], "PKCE must be on"
        return client.get(CALLBACK, {"code": "c", "state": query["state"][0]})


def invite(tenancy: Any, email: str = "joiner@example.test") -> Invitation:
    return create_invitation(
        org=tenancy.organization,
        email=email,
        org_role=OrgRole.MEMBER,
        workspace_assignments=[{"workspace_id": str(tenancy.workspace.pk), "role": WorkspaceRole.AGENT.value}],
        invited_by=tenancy.owner,
    )


def hold(client: Client, invitation: Invitation) -> None:
    """What opening the invitation link does: park the token in the session."""
    assert client.get(reverse("accept_invite", kwargs={"token": invitation.raw_token})).status_code == 200


def orgs() -> int:
    return Organization.objects.count()


@pytest.mark.django_db
class TestTheButton:
    def test_absent_without_credentials(self, client: Client) -> None:
        assert b"Zirek" not in client.get(LOGIN).content

    def test_present_when_configured_and_google_is_not_dragged_in(self, client: Client, sso: None) -> None:
        body = client.get(LOGIN).content

        assert b"Continue with Zirek" in body
        assert b"Continue with Google" not in body


@pytest.mark.django_db
class TestNewIdentities:
    def test_no_invitation_means_no_account_and_no_organization(self, client: Client, sso: None, tenancy: Any) -> None:
        before = orgs()

        response = sign_in(client)

        assert response.status_code == 200
        assert b"Sign up is closed" in response.content
        assert not SocialAccount.objects.exists()
        assert orgs() == before

    def test_an_invitation_with_a_verified_matching_address_joins_that_organization(
        self, client: Client, sso: None, tenancy: Any
    ) -> None:
        invitation = invite(tenancy)
        hold(client, invitation)
        before = orgs()

        response = sign_in(client)

        assert response.status_code == 302
        membership = OrgMembership.objects.get(user__email="joiner@example.test")
        assert membership.organization == tenancy.organization
        assert OrgMembership.objects.filter(user=membership.user).count() == 1
        assert orgs() == before, "an invited user must not get an organization of their own"
        assert SocialAccount.objects.get(provider="sso").uid == "sub-1"
        invitation.refresh_from_db()
        assert invitation.is_accepted
        assert client.get("/").status_code == 302
        assert orgs() == before, "ensure_provisioned must have nothing left to do"

    @pytest.mark.parametrize(
        "claims",
        [
            {"email_verified": False},
            {"email": "someone.else@example.test"},
            {"email_verified": None},
        ],
    )
    def test_an_unverified_or_different_address_is_refused(
        self, client: Client, sso: None, tenancy: Any, claims: dict[str, Any]
    ) -> None:
        hold(client, invite(tenancy))

        response = sign_in(client, **claims)

        assert b"Sign up is closed" in response.content
        assert not SocialAccount.objects.exists()
        assert not OrgMembership.objects.filter(user__email="someone.else@example.test").exists()

    def test_case_does_not_matter(self, client: Client, sso: None, tenancy: Any) -> None:
        hold(client, invite(tenancy))

        sign_in(client, email="Joiner@Example.Test")

        assert SocialAccount.objects.filter(provider="sso").exists()

    @pytest.mark.parametrize(
        ("failure", "shown"),
        [
            (MembershipError("No seats left."), b"No seats left."),
            (IntegrityError("duplicate key"), b"just created"),
        ],
    )
    def test_a_refused_invitation_leaves_nothing_behind(
        self, client: Client, sso: None, tenancy: Any, failure: Exception, shown: bytes
    ) -> None:
        """The seat is gone, or a twin request won, by the time the identity
        arrives. The user row must roll back with it: ``ensure_provisioned`` gives
        an organization to anyone signed in without one."""
        hold(client, invite(tenancy))
        before = orgs()

        with mock.patch("apps.accounts.adapters.accept_invitation", side_effect=failure):
            response = sign_in(client)

        assert response.status_code == 403
        assert shown in response.content
        assert not SocialAccount.objects.exists()
        assert not get_user_model().objects.filter(email="joiner@example.test").exists()
        assert orgs() == before
        assert PENDING_INVITE_SESSION_KEY in client.session, "a refusal must not cost the visitor the invitation"

    def test_an_expired_token_is_no_better_than_none(self, client: Client, sso: None, tenancy: Any) -> None:
        invitation = invite(tenancy)
        hold(client, invitation)
        Invitation.objects.filter(pk=invitation.pk).update(expires_at="2000-01-01T00:00:00Z")

        response = sign_in(client)

        assert b"Sign up is closed" in response.content
        assert not SocialAccount.objects.exists()


@pytest.mark.django_db
class TestKnownIdentities:
    def test_a_linked_identity_signs_in_without_an_invitation(self, client: Client, sso: None, tenancy: Any) -> None:
        user = tenancy.owner
        SocialAccount.objects.create(user=user, provider="sso", uid="sub-1")

        response = sign_in(client, email=user.email)

        assert response.status_code == 302
        assert client.session["_auth_user_id"] == str(user.pk)

    def test_a_local_account_with_the_same_address_signs_in_and_is_linked(
        self, client: Client, sso: None, tenancy: Any
    ) -> None:
        local = tenancy.owner

        response = sign_in(client, email=local.email)

        assert response.status_code == 302
        assert client.session["_auth_user_id"] == str(local.pk)
        assert SocialAccount.objects.get(provider="sso", uid="sub-1").user == local

    def test_the_second_sign_in_no_longer_depends_on_the_address(self, client: Client, sso: None, tenancy: Any) -> None:
        local = tenancy.owner
        sign_in(client, email=local.email)
        client.logout()

        sign_in(client, email="moved@example.test")

        assert client.session["_auth_user_id"] == str(local.pk)

    def test_an_unverified_local_account_loses_its_password(self, client: Client, sso: None, tenancy: Any) -> None:
        """Pre-registering someone else's address must not leave you a key to the
        account they are about to be signed into."""
        local = tenancy.owner
        assert local.has_usable_password()

        sign_in(client, email=local.email)

        local.refresh_from_db()
        assert not local.has_usable_password()

    def test_without_link_by_email_the_visitor_is_told_the_way_out(
        self, client: Client, sso: None, tenancy: Any, settings: Any
    ) -> None:
        """No auto-connect: ACCOUNT_EMAIL_VERIFICATION is optional, so a local
        account can hold an address its owner never proved."""
        providers = copy.deepcopy(settings.SOCIALACCOUNT_PROVIDERS)
        providers["openid_connect"]["APPS"][0]["settings"]["email_authentication"] = False
        settings.SOCIALACCOUNT_PROVIDERS = providers
        local = create_user("joiner@example.test")
        hold(client, invite(tenancy))

        response = sign_in(client)

        assert response.status_code == 403
        assert b"already exists" in response.content
        assert "_auth_user_id" not in client.session
        assert not SocialAccount.objects.filter(user=local).exists()


@pytest.mark.django_db
class TestPlatformProvisionedAccount:
    """The platform API creates accounts with no invitation behind them. They
    must still be able to sign in through SSO, which is the only way in."""

    def _provision(self) -> Any:
        from apps.organizations.services import create_organization

        org, _ = create_organization(name="Acme", owner_email="Boss@Example.test")
        return OrgMembership.objects.get(organization=org).user

    def test_it_signs_in_and_is_linked_with_no_invitation(self, client: Client, sso: None) -> None:
        owner = self._provision()

        response = sign_in(client, email="boss@example.test")

        assert response.status_code == 302
        assert client.session["_auth_user_id"] == str(owner.pk)
        assert SocialAccount.objects.get(provider="sso", uid="sub-1").user == owner
        assert Organization.objects.count() == 1, "signing in must not provision a second organization"

    def test_without_link_by_email_it_is_refused(self, client: Client, sso: None, settings: Any) -> None:
        """Documented in docs/api/platform-v1.md: platform provisioning needs
        OIDC_LINK_BY_EMAIL, or the provisioned account cannot be reached."""
        providers = copy.deepcopy(settings.SOCIALACCOUNT_PROVIDERS)
        providers["openid_connect"]["APPS"][0]["settings"]["email_authentication"] = False
        settings.SOCIALACCOUNT_PROVIDERS = providers
        self._provision()

        response = sign_in(client, email="boss@example.test")

        assert response.status_code == 403
        assert "_auth_user_id" not in client.session


@pytest.mark.django_db
class TestTheSignupFormPath:
    """allauth's signup form lets the visitor edit the address. Nothing in the
    callback reaches the form once the checks above are in place, so ``save_user``
    is exercised directly with the form's data."""

    def _request(self, client: Client) -> Any:
        from django.test import RequestFactory

        request = RequestFactory().post("/accounts/3rdparty/signup/")
        request.session = client.session
        return request

    def _sociallogin(self, request: Any) -> Any:
        from allauth.socialaccount.adapter import get_adapter

        provider = get_adapter().get_provider(request, provider="sso")
        return provider.sociallogin_from_response(
            request, {"userinfo": {"sub": "sub-9", "email": "joiner@example.test", "email_verified": True}}
        )

    def test_an_edited_address_does_not_become_the_accounts_address(
        self, client: Client, sso: None, tenancy: Any
    ) -> None:
        from apps.accounts.adapters import SocialAccountAdapter

        hold(client, invite(tenancy))
        request = self._request(client)
        form = SimpleNamespace(cleaned_data={"email": "edited@example.test"})

        user = SocialAccountAdapter().save_user(request, self._sociallogin(request), form=form)

        assert user.email == "joiner@example.test", "the provider-verified address wins over the form's"
        assert OrgMembership.objects.get(user=user).organization == tenancy.organization

    def test_a_mismatch_is_refused_whatever_allauth_does_with_the_address(
        self, client: Client, sso: None, tenancy: Any
    ) -> None:
        from allauth.core.exceptions import ImmediateHttpResponse

        from apps.accounts.adapters import SocialAccountAdapter

        hold(client, invite(tenancy, email="invited@example.test"))
        request = self._request(client)
        before = orgs()

        with pytest.raises(ImmediateHttpResponse) as caught:
            SocialAccountAdapter().save_user(request, self._sociallogin(request))

        assert caught.value.response.status_code == 403
        assert not get_user_model().objects.filter(email="joiner@example.test").exists()
        assert not SocialAccount.objects.exists()
        assert orgs() == before


@pytest.mark.django_db
class TestTheSignalFailsLoudly:
    def test_an_sso_signup_without_a_membership_is_logged(self, sso: None, caplog: Any) -> None:
        from allauth.socialaccount.models import SocialAccount as Account

        from apps.accounts.signals import provision_on_signup

        user = create_user("orphan@example.test")
        sociallogin = mock.Mock(account=Account(provider="sso", uid="x"))

        provision_on_signup(sender=type(user), request=None, user=user, sociallogin=sociallogin)

        assert "without an organization membership" in caplog.text
        assert not OrgMembership.objects.filter(user=user).exists(), "the receiver must not provision one"


@pytest.mark.django_db
class TestOtherProviders:
    """The code must not depend on which OpenID Connect provider is configured."""

    def test_a_provider_that_never_sends_email_verified_can_be_trusted_by_the_operator(
        self, client: Client, sso: None, tenancy: Any, settings: Any
    ) -> None:
        settings.OIDC_TRUST_EMAIL = True
        hold(client, invite(tenancy))

        sign_in(client, email_verified=None)

        assert OrgMembership.objects.get(user__email="joiner@example.test").organization == tenancy.organization

    def test_trusting_the_email_does_not_waive_the_invitation(
        self, client: Client, sso: None, tenancy: Any, settings: Any
    ) -> None:
        settings.OIDC_TRUST_EMAIL = True

        response = sign_in(client, email_verified=None)

        assert b"Sign up is closed" in response.content
        assert not SocialAccount.objects.exists()

    def test_open_signup_gives_a_new_identity_an_organization_of_its_own(
        self, client: Client, sso: None, tenancy: Any, settings: Any
    ) -> None:
        settings.OIDC_SIGNUP = "open"
        before = orgs()

        response = sign_in(client)

        assert response.status_code == 302
        assert SocialAccount.objects.get(provider="sso").uid == "sub-1"
        assert OrgMembership.objects.filter(user__email="joiner@example.test").count() == 1
        assert orgs() == before + 1

    def test_open_signup_still_honours_an_invitation(
        self, client: Client, sso: None, tenancy: Any, settings: Any
    ) -> None:
        settings.OIDC_SIGNUP = "open"
        hold(client, invite(tenancy))
        before = orgs()

        sign_in(client)

        assert OrgMembership.objects.get(user__email="joiner@example.test").organization == tenancy.organization
        assert orgs() == before


class TestTheConfiguration:
    def test_disabled_without_all_three_values(self, settings: Any) -> None:
        assert settings.OIDC_CONFIGURED is False
        assert settings.SOCIALACCOUNT_PROVIDERS["openid_connect"]["APPS"] == []

    def test_login_still_requires_post_and_never_auto_connects(self, settings: Any) -> None:
        assert settings.SOCIALACCOUNT_LOGIN_ON_GET is False
        assert getattr(settings, "SOCIALACCOUNT_EMAIL_AUTHENTICATION", False) is False


class TestTheRedirectIsPermittedByCsp:
    @pytest.mark.parametrize(
        ("issuer", "origin"),
        [(ISSUER, "https://auth.example.test"), ("http://localhost:8443/auth/v1", "http://localhost:8443")],
    )
    def test_the_issuer_origin_is_what_form_action_gets(self, issuer: str, origin: str) -> None:
        from config.settings.base import csp_origin

        assert csp_origin(issuer) == origin
