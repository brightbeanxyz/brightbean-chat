"""allauth adapters."""

import logging
import smtplib
from typing import Any

from allauth.account.adapter import DefaultAccountAdapter
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from django.contrib import messages
from django.http import HttpRequest

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
