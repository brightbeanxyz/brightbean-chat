"""Signup that knows about pending invitations."""

from typing import Any

from allauth.account.views import SignupView

from apps.members.models import Invitation
from apps.members.signals_keys import PENDING_INVITE_SESSION_KEY


class InvitePrefillSignupView(SignupView):
    """Pre-fill the email when the session carries a pending invite token.

    The field is rendered read-only, but that is a convenience, not a control:
    the **token** is the authorization, which is why
    ``accept_invitation(..., require_email_match=False)`` runs on this path.
    Someone who edits the DOM and signs up with a different address still joins
    the organization the token names — deliberately, because a social login
    returns whatever address the provider owns and refusing it would strand
    invitees. Ported from Studio with the trade made explicit rather than
    implied.
    """

    def _invited_email(self) -> str | None:
        token = self.request.session.get(PENDING_INVITE_SESSION_KEY)
        if not token:
            return None
        invitation = Invitation.objects.filter(token=token, accepted_at__isnull=True).first()
        if invitation and not invitation.is_expired:
            return invitation.email
        return None

    def get_initial(self) -> dict[str, Any]:
        initial = super().get_initial()
        email = self._invited_email()
        if email:
            initial["email"] = email
        return initial

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context: dict[str, Any] = super().get_context_data(**kwargs)
        context["invited_email_locked"] = bool(self._invited_email())
        return context
