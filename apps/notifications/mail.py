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
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from apps.notifications.models import DeliveryStatus, NotificationDelivery

logger = logging.getLogger(__name__)

__all__ = ["app_url", "send_delivery"]


def app_url() -> str:
    """The deployment's base URL, without a trailing slash."""
    return str(getattr(settings, "APP_URL", "http://localhost:8000")).rstrip("/")


def _action_url(notification: Any) -> str:
    """Where the email's button goes.

    ``payload["action_url"]`` is written by whichever layer called ``notify()``
    and may be absolute already; anything else falls back to the notification
    list, which always exists.
    """
    raw = notification.payload.get("action_url") if isinstance(notification.payload, dict) else None
    if isinstance(raw, str) and raw.startswith(("http://", "https://")):
        return raw
    base = app_url()
    if isinstance(raw, str) and raw.startswith("/"):
        return f"{base}{raw}"
    return f"{base}{reverse('notifications:list')}"


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
            subject=notification.payload.get("email_subject") or notification.title,
            body=render_to_string("notifications/email/notification.txt", context),
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[recipient.email],
        )
        message.attach_alternative(render_to_string("notifications/email/notification.html", context), "text/html")
        message.send()
    except (OSError, smtplib.SMTPException) as exc:
        # No address in the log line: the recipient is personal data, and
        # apps/accounts/adapters.py makes the same omission for the same reason.
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
