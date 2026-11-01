"""allauth adapters."""

import logging
import smtplib
from collections.abc import Callable
from typing import Any

from allauth.account.adapter import DefaultAccountAdapter
from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.http import HttpRequest
from django.shortcuts import render
from django.utils.translation import gettext

from apps.accounts.services import is_invite_only, is_sso, pending_invitation, signup_is_open
from apps.members.services import MembershipError, accept_invitation
from apps.members.signals_keys import PENDING_INVITE_SESSION_KEY

logger = logging.getLogger(__name__)


class AccountAdapter(DefaultAccountAdapter):
    """Two narrow edits to allauth's defaults: what it says, and what it swallows.

    **What it says** is ``add_message`` below — allauth's own flash notices are
    dropped unless they report a failure.

    **What it swallows** is the rest of this class: never let a mail failure
    become a failed signup.

    ``ACCOUNT_EMAIL_VERIFICATION = "optional"`` means a verification email is
    sent at signup but never gates access (brief, deviation 7). The point of
    that setting is that a self-hoster who has not configured SMTP is not locked
    out of their own instance — and an unhandled ``SMTPException`` inside
    ``complete_signup`` would lock them out just as hard, with a 500 instead of
    a login wall, *after* creating the account.

    So delivery failures are logged and swallowed. The same applies to password
    reset: the response is already deliberately identical whether or not the
    address exists (SECURITY-BASELINE §8), so a 500 there would leak more than
    it protects.

    Studio has no account adapter at all; it ships ``"none"`` and never sends.
    """

    def add_message(
        self,
        request: HttpRequest,
        level: int,
        message_template: str | None = None,
        message_context: dict[str, Any] | None = None,
        extra_tags: str = "",
        message: str | None = None,
    ) -> None:
        """Drop allauth's narration; keep anything it reports as a failure.

        "Confirmation email sent to you" and "Successfully signed in as you",
        stacked on the landing page you just asked for, above a sidebar already
        showing that address, are the first thing a new account sees. They
        describe what the user just did rather than telling them anything, and
        they push the page's own content down to do it.

        The cut is the *level* rather than a list of template names, because the
        list is allauth's to change and a name it adds in a future release would
        otherwise reappear unannounced. A notice raised at ERROR is feedback on
        something that did not work and the user has no other way to learn it.

        Twelve call sites in the two installed apps raise at that level, and a
        name-based list would have missed most of them: four render a template
        (three under ``/accounts/email/``, which ``allauth.urls`` mounts — the
        primary address cannot be deleted, the primary address must be verified,
        a confirmation link belongs to a different account — plus the social
        account already connected elsewhere), and the other seven pass a literal
        ``message=`` from ``error_messages``: every rate-limit and
        too-many-login-attempts notice in ``allauth/account/views.py``. Throttled
        sign-ins are exactly the case where silence looks like a broken form.

        Every notice in both installed allauth apps arrives here, including
        ``socialaccount``'s — its flows call ``get_account_adapter()`` too — so
        this is the one place the decision is made rather than eighteen empty
        message templates. ``allauth.mfa`` and ``allauth.usersessions`` are not
        installed.

        Studio shows all of them; it also has no landing page worth protecting.
        """
        if level < messages.ERROR:
            return
        super().add_message(request, level, message_template, message_context, extra_tags, message)

    def is_open_for_signup(self, request: HttpRequest) -> bool:
        # allauth asks this for the local form and, through the default
        # SocialAccountAdapter, for a social login that is not invite-only; a
        # refusal renders signup_closed.html.
        return signup_is_open(request)

    def save_user(self, request: HttpRequest, user: Any, form: Any, commit: bool = True) -> Any:
        """Under ``SIGNUP=invite`` the account and the invitation are one transaction.

        ``signup_is_open`` only reads the invitation, so on its own it cannot
        stop a link shared with a second person: both pass it, the first spends
        the invitation, and the second would be left with an account and,
        through ``provision_on_signup``, an organization of their own. Accepting
        here, with the address matched against the invited one (the form field
        is read-only only in the browser), means a refusal leaves no account.

        A social signup form also arrives here, but its adapter does this itself.
        """
        if not is_invite_only() or getattr(form, "sociallogin", None) is not None:
            return super().save_user(request, user, form, commit)
        return _create_user_and_accept_invitation(
            request, lambda: super(AccountAdapter, self).save_user(request, user, form, commit)
        )

    def send_mail(self, template_prefix: str, email: str, context: dict[str, Any]) -> None:
        try:
            super().send_mail(template_prefix, email, context)
        except (OSError, smtplib.SMTPException):
            # Delivery failures only. A TemplateSyntaxError or a renamed context
            # key must not be swallowed here and reported as an SMTP problem —
            # that turns a broken email template into an invisible non-delivery
            # with a message pointing operators at settings that are fine.
            #
            # No address in the log line: the recipient is personal data, and
            # the template prefix is enough to say which mail failed.
            logger.exception("Failed to send account email (%s). Check the SMTP settings.", template_prefix)


def _signup_closed(request: HttpRequest, reason: str) -> ImmediateHttpResponse:
    return ImmediateHttpResponse(render(request, "account/signup_closed.html", {"reason": reason}, status=403))


def _create_user_and_accept_invitation(request: HttpRequest, create_user: Callable[[], Any]) -> Any:
    """Create the account and accept the pending invitation, or neither.

    The one place both invite-only signups (the local form and a social login)
    go through, so a fix to the race or to the refusal lands in both. The address
    must be the invited one: the form field is read-only only in the browser, and
    a provider's address is whatever it vouches for.
    """
    invitation = pending_invitation(request)
    try:
        if invitation is None:
            raise MembershipError(gettext("An invitation is required to sign up."))
        with transaction.atomic():
            user = create_user()
            accept_invitation(invitation, user, require_email_match=True)
    except MembershipError as exc:
        raise _signup_closed(request, str(exc)) from exc
    except IntegrityError as exc:
        # Two requests for one address (or identity) raced; the other one won.
        logger.warning("Invite-only signup lost a uniqueness race")
        raise _signup_closed(request, gettext("This account was just created. Try signing in again.")) from exc
    request.session.pop(PENDING_INVITE_SESSION_KEY, None)
    return user


class SocialAccountAdapter(DefaultSocialAccountAdapter):
    """Map a provider profile onto this project's single ``name`` field, and
    decide who may be created through single sign-on.

    Ported from Studio's adapter minus its ``OAuthConnection`` bookkeeping —
    that model duplicates allauth's own ``SocialAccount`` table.

    An OIDC provider may serve many tenants, so signing in there says who someone
    is and nothing about which organization is theirs. With the default
    ``OIDC_SIGNUP=invite``, an identity already linked (a ``SocialAccount`` row)
    signs in without passing through here, and a new one is created only to
    honour an invitation, to the address it was sent to — never with an
    organization of its own, which Google sign-ups get. ``open`` restores that.
    """

    def pre_social_login(self, request: HttpRequest, sociallogin: Any) -> None:
        """Say why, when the invited address already belongs to a local account.

        allauth would otherwise send the visitor to a signup form whose email
        field is "already taken" — a dead end that names no way out. Linking is
        deliberately not automatic (see the note beside SOCIALACCOUNT_ADAPTER in
        settings), so the way out is to sign in locally and connect SSO.
        """
        if not is_sso(sociallogin):
            return
        if settings.OIDC_TRUST_EMAIL:
            for address in sociallogin.email_addresses:
                address.verified = True
        if is_invite_only(sociallogin) and not sociallogin.is_existing:
            addresses = [a.email for a in sociallogin.email_addresses if a.verified]
            if addresses and get_user_model().objects.filter(email__iexact=addresses[0]).exists():
                raise _signup_closed(
                    request,
                    gettext(
                        "An account with this address already exists. Sign in with your password, "
                        "accept the invitation, then connect %(name)s under your connected accounts."
                    )
                    % {"name": settings.OIDC_NAME},
                )

    def is_open_for_signup(self, request: HttpRequest, sociallogin: Any) -> bool:
        if not is_invite_only(sociallogin):
            return super().is_open_for_signup(request, sociallogin)
        invitation = pending_invitation(request)
        if invitation is None:
            return False
        invited = invitation.email.strip().lower()
        return any(a.verified and a.email.strip().lower() == invited for a in sociallogin.email_addresses)

    def save_user(self, request: HttpRequest, sociallogin: Any, form: Any = None) -> Any:
        if not is_invite_only(sociallogin):
            return super().save_user(request, sociallogin, form)
        # The user and the membership are one transaction. ``ensure_provisioned``
        # gives a fresh organization to anyone signed in without one, so a user
        # left behind by a refused invitation would get exactly what this
        # provider exists to withhold.
        return _create_user_and_accept_invitation(
            request, lambda: super(SocialAccountAdapter, self).save_user(request, sociallogin, form)
        )

    def populate_user(self, request: Any, sociallogin: Any, data: dict[str, Any]) -> Any:
        user = super().populate_user(request, sociallogin, data)
        full_name = f"{data.get('first_name', '')} {data.get('last_name', '')}".strip() or (data.get("name") or "")
        if full_name and not user.name:
            user.name = full_name
        return user
