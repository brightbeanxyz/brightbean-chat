"""Signup wiring.

One receiver, on allauth's ``user_signed_up``. See
:mod:`apps.accounts.services` for why there is no ``post_save`` receiver.
"""

import logging
from typing import Any

from allauth.account.signals import user_signed_up
from django.dispatch import receiver

from apps.accounts.services import provision_organization_and_workspace
from apps.members.signals_keys import PENDING_INVITE_SESSION_KEY

logger = logging.getLogger(__name__)


@receiver(user_signed_up)
def provision_on_signup(sender: Any, request: Any, user: Any, **kwargs: Any) -> None:
    """Join the inviting organization, or create a fresh one.

    The invite token is put in the session by the public accept-invite view, so
    it survives the round trip through the signup form (and through Google).
    """
    from apps.members.models import Invitation
    from apps.members.services import MembershipError, accept_invitation

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
