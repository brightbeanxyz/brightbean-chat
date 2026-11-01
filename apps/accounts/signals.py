"""Signup wiring.

One receiver, on allauth's ``user_signed_up``. See
:mod:`apps.accounts.services` for why there is no ``post_save`` receiver.

The account's language preference has no signal of its own to wire up:
``apps.accounts.middleware.LanguagePreferenceMiddleware`` reads
``request.user.language`` fresh on every request instead of trying to carry it
forward from login — this Django version has no session-based hook left for
that (``LANGUAGE_SESSION_KEY`` is gone), and re-reading it is no more
expensive than the query ``AuthenticationMiddleware`` already ran to produce
``request.user``.
"""

import logging
from typing import Any

from allauth.account.signals import user_logged_in, user_signed_up
from django.dispatch import receiver

from apps.accounts.services import is_invite_only, is_sso, provision_organization_and_workspace
from apps.members.signals_keys import PENDING_INVITE_SESSION_KEY

logger = logging.getLogger(__name__)


@receiver(user_logged_in)
def link_sso_identity(sender: Any, request: Any, user: Any, **kwargs: Any) -> None:
    """Pin the provider's ``sub`` to a local account that was signed in by email.

    allauth's per-provider ``email_authentication`` signs the person in but leaves
    the identity unlinked, so every later login would depend on the address still
    belonging to them. Linking makes the first match the last.
    """
    sociallogin = kwargs.get("sociallogin")
    if sociallogin is not None and is_sso(sociallogin) and sociallogin.account.pk is None:
        sociallogin.connect(request, user)


@receiver(user_signed_up)
def provision_on_signup(sender: Any, request: Any, user: Any, **kwargs: Any) -> None:
    """Join the inviting organization, or create a fresh one.

    The invite token is put in the session by the public accept-invite view, so
    it survives the round trip through the signup form (and through Google).
    """
    from apps.members.models import Invitation
    from apps.members.services import MembershipError, accept_invitation

    sociallogin = kwargs.get("sociallogin")
    if is_invite_only(sociallogin):
        # The adapter's save_user already joined the inviting organization,
        # in the same transaction as the user. Falling through would, on any
        # miss, hand an SSO user an organization of their own. If that ever
        # stops being true, say so loudly: ensure_provisioned would otherwise
        # fix it up silently on the first page view.
        from apps.members.models import OrgMembership

        if not OrgMembership.objects.filter(user=user).exists():
            logger.error("Invite-only sign-up for user %s finished without an organization membership", user.pk)
        return

    token = request.session.pop(PENDING_INVITE_SESSION_KEY, None) if request is not None else None
    if token:
        invitation = Invitation.objects.for_token(token).filter(accepted_at__isnull=True).first()
        if invitation is not None and not invitation.is_expired:
            try:
                # The session-bound token is itself proof the invitation reached
                # its recipient, and a social login returns whatever address the
                # provider owns — which need not be the invited one.
                accept_invitation(invitation, user, require_email_match=False)
                return
            except MembershipError:
                logger.warning("Pending invitation %s could not be accepted at signup", invitation.pk)

    provision_organization_and_workspace(user)
