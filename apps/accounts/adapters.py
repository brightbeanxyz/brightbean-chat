"""allauth adapters."""

import logging
import smtplib
from typing import Any

from allauth.account.adapter import DefaultAccountAdapter
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter

logger = logging.getLogger(__name__)


class AccountAdapter(DefaultAccountAdapter):
    """Never let a mail failure become a failed signup.

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


class SocialAccountAdapter(DefaultSocialAccountAdapter):
    """Map a Google profile onto this project's single ``name`` field.

    Ported from Studio's adapter minus its ``OAuthConnection`` bookkeeping —
    that model duplicates allauth's own ``SocialAccount`` table.
    """

    def populate_user(self, request: Any, sociallogin: Any, data: dict[str, Any]) -> Any:
        user = super().populate_user(request, sociallogin, data)
        full_name = f"{data.get('first_name', '')} {data.get('last_name', '')}".strip()
        if full_name and not user.name:
            user.name = full_name
        return user
