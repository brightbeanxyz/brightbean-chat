"""Sending one notification email.

Follows the house pattern set by ``apps.members.services.send_invite_email``:
subject built in Python, the ``.txt`` body as the message body with the
``.html`` attached as an alternative, ``settings.DEFAULT_FROM_EMAIL`` as the
sender, and absolute URLs built from ``settings.APP_URL``.

It also copies that function's **narrow** exception clause, which is the part
worth copying deliberately. ``except Exception`` here would report a
``TemplateSyntaxError`` or a renamed context key as a mail-delivery problem and
then swallow it: the delivery row would read ``failed`` with an SMTP-shaped
message, and every notification email in the deployment would be silently
missing. Only transport errors are caught.
"""

import logging
import smtplib
from typing import Any

from django.conf import settings
from django.core.mail import BadHeaderError, EmailMultiAlternatives
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from apps.notifications import action_urls
from apps.notifications.action_urls import app_url
from apps.notifications.models import DeliveryStatus, NotificationDelivery

logger = logging.getLogger(__name__)

__all__ = ["send_delivery"]


def _payload_of(notification: Any) -> dict[str, Any]:
    """The row's payload, or an empty dict.

    ``payload`` is a ``JSONField``, so a row written by a future caller — or
    edited in the admin — can hold a list or a string. Normalising once here
    means the subject line and the action URL cannot disagree about whether
    that is possible, which they did: one guarded with ``isinstance`` and the
    other called ``.get()`` straight out.
    """
    payload = getattr(notification, "payload", None)
    return payload if isinstance(payload, dict) else {}


def _subject(notification: Any) -> str:
    """The subject line, with anything that would break a header removed.

    A context value carrying a newline reaches the title through the copy
    templates, and ``Notification.title`` is a varchar that stores it happily.
    Django then raises ``BadHeaderError`` at send time — after this function's
    caller has already incremented ``attempts`` in memory and before it saves,
    so the delivery row would sit at PENDING with no attempt recorded, looking
    queued rather than broken. Collapsing the whitespace is both the fix and
    the better email.
    """
    raw = str(_payload_of(notification).get("email_subject") or notification.title)
    return " ".join(raw.split())


def _action_url(notification: Any) -> str:
    """Where the email's button goes.

    ``notify()`` already reduced ``payload["action_url"]`` to a same-origin path
    (or dropped it), so this re-checks rather than validates: a row edited in
    the admin, or written before this rule existed, must not be able to put an
    off-site link in an email that carries this product's branding. One policy,
    in :mod:`apps.notifications.action_urls`, applied at both ends.
    """
    return action_urls.absolute(action_urls.safe_path(_payload_of(notification).get("action_url")))


def send_delivery(delivery: NotificationDelivery) -> bool:
    """Send the email this delivery stands for. Returns whether it went out.

    Records the outcome on the row either way. Retries are not this function's
    business — issue #5's queue owns backoff (SPEC §15), and ``attempts`` here
    is an observation rather than an input to a decision.
    """
    notification = delivery.notification
    recipient = notification.user

    delivery.attempts += 1

    context = {
        "notification": notification,
        "user": recipient,
        "action_url": _action_url(notification),
        "app_url": app_url(),
        "settings_url": f"{app_url()}{reverse('notifications:list')}",
    }

    try:
        message = EmailMultiAlternatives(
            subject=_subject(notification),
            body=render_to_string("notifications/email/notification.txt", context),
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[recipient.email],
        )
        message.attach_alternative(render_to_string("notifications/email/notification.html", context), "text/html")
        message.send()
    except (OSError, smtplib.SMTPException, BadHeaderError) as exc:
        # No address in the log line: the recipient is personal data, and
        # apps/accounts/adapters.py makes the same omission for the same reason.
        #
        # BadHeaderError is in the clause because it is a ValueError and would
        # otherwise escape *between* the in-memory attempts increment and the
        # save below, leaving the row PENDING with attempts=0 — indistinguishable
        # from one that was never tried. Named, not widened to ValueError, so a
        # genuine coding error still surfaces.
        logger.exception("Failed to send notification email for notification %s", notification.pk)
        delivery.status = DeliveryStatus.FAILED
        delivery.error_message = str(exc)[:500]
        delivery.save(update_fields=["status", "error_message", "attempts", "updated_at"])
        return False

    delivery.status = DeliveryStatus.SENT
    delivery.sent_at = timezone.now()
    delivery.error_message = ""
    delivery.save(update_fields=["status", "sent_at", "error_message", "attempts", "updated_at"])
    return True
