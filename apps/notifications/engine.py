"""The single entry point every feature calls to reach a human.

    from apps.notifications.engine import notify

    notify(
        workspace,
        "flow_loop_cap_hit",
        roles=("admin",),
        context={"flow_name": flow.name, "contact_name": contact.display_name},
    )

The issue writes this helper as ``notifications.notify(...)``. It is **not**
re-exported from ``apps/notifications/__init__.py``: every app package in this
repo has an empty ``__init__``, and Django imports app packages during
``apps.populate()`` — before the model registry exists — so a package-level
``from .engine import notify`` would drag models in and raise
``AppRegistryNotReady``. ``from apps.notifications.engine import notify`` is the
public import, which is also what Studio's own module docstring instructs.

There are no callers in this issue. Layers 3 to 6 supply them.
"""

import logging
from collections.abc import Iterable, Sequence
from functools import partial
from typing import Any

from django.db import transaction

from apps.notifications import action_urls, events, queue
from apps.notifications.mail import send_delivery
from apps.notifications.models import (
    Channel,
    DeliveryStatus,
    Notification,
    NotificationDelivery,
    NotificationSetting,
)
from apps.notifications.recipients import DEFAULT_ROLES, active_users, recipients_for_roles

logger = logging.getLogger(__name__)

__all__ = ["notify"]


def notify(
    workspace: Any,
    event_type: str,
    *,
    users: Iterable[Any] | None = None,
    roles: Sequence[str] | None = None,
    context: dict[str, Any] | None = None,
) -> list[Notification]:
    """Create in-app notifications, and email the ones that earn an email.

    Recipients come from exactly one of two places:

    * ``users`` — an explicit list. Deduplicated, deactivated accounts dropped.
      ``users=[]`` means **nobody**, and is deliberately distinguishable from
      ``users=None``: an empty recipient list quietly turning into "all admins"
      is the failure mode worth being strict about.
    * ``roles`` — resolved against ``workspace``. Defaults to
      ``("admin",)``, which is SPEC §9.2's "notify workspace admins".

    Passing both is a ``TypeError`` in every environment. It is a call-signature
    error rather than a runtime condition, so it does not go through the
    DEBUG-raise/production-log policy that unknown event types do. Both
    parameters default to ``None`` rather than ``roles`` defaulting to
    ``("admin",)`` directly, because that is what makes "both were given"
    detectable at all.

    Returns the created rows, in recipient order. A count is strictly less
    information (``len()`` recovers it) and ``None`` would force every caller
    and test to re-query.

    No transaction is opened here on purpose. The flow engine (SPEC §9.6) runs
    inside one, and a notification must not be able to roll back a flow step.
    The synchronous email send is deferred with ``transaction.on_commit`` so a
    caller whose transaction *does* roll back has not already mailed anyone; the
    enqueue path needs no such guard, because the queue row is written in the
    same transaction and rolls back with it.
    """
    if users is not None and roles is not None:
        raise TypeError(
            "notify() takes users= or roles=, not both. Pass the people you mean, "
            "or the roles to resolve them from — passing both leaves it ambiguous "
            "which one the caller believed was in effect."
        )

    event = events.get_event(event_type)
    if event is None:
        # Production path for an unregistered type: events.get_event has already
        # logged, and raising here would fail the webhook that triggered it.
        return []

    context = context or {}
    recipients = active_users(users) if users is not None else recipients_for_roles(workspace, roles or DEFAULT_ROLES)
    if not recipients:
        return []

    title, body = events.render(event, context)
    payload = _payload(workspace, event, context)

    notifications = [
        Notification.objects.create(
            user=recipient,
            event_type=event.key,
            title=title,
            body=body,
            payload=payload,
        )
        for recipient in recipients
    ]

    if event.emails_by_default:
        _dispatch_emails(workspace, notifications)

    return notifications


def _payload(workspace: Any, event: events.NotificationEvent, context: dict[str, Any]) -> dict[str, Any]:
    """What the row carries besides its copy.

    ``workspace_id`` is denormalised rather than a foreign key: notifications
    are per-user and read across every workspace a person belongs to, so the
    model stays out of ``WorkspaceScopedModel``'s way (see ``models`` docstring).
    Values are coerced through ``str`` where they are ids, because this column is
    ``jsonb`` and a UUID is not JSON-serialisable.
    """
    payload: dict[str, Any] = {key: value for key, value in context.items() if _json_safe(value)}

    # action_url is the one context key that becomes a link rather than text,
    # so it is the one that has to be checked rather than merely escaped —
    # escaping does nothing to a scheme. Enforced here, at the single write
    # path, so the bell, the history page and the email button all inherit it
    # instead of each remembering. See apps.notifications.action_urls.
    if "action_url" in payload:
        safe = action_urls.safe_path(payload["action_url"])
        if safe is None:
            del payload["action_url"]
        else:
            payload["action_url"] = safe

    payload["workspace_id"] = str(getattr(workspace, "pk", workspace)) if workspace is not None else None
    payload["workspace_name"] = getattr(workspace, "name", "")
    payload["icon"] = event.icon
    payload["tone"] = event.tone
    if event.email_subject:
        # An event may want a subject line that differs from the in-app title
        # ("Your flow stopped" reads oddly in an inbox). It is a copy template
        # like the others, so it is filled from the same context.
        payload["email_subject"] = events.render(
            events.NotificationEvent(key=event.key, label=event.label, icon=event.icon, title=event.email_subject),
            context,
        )[0]
    return payload


def _json_safe(value: Any) -> bool:
    return isinstance(value, str | int | float | bool | list | dict) or value is None


def _dispatch_emails(workspace: Any, notifications: list[Notification]) -> None:
    """Queue or send one email per notification, skipping people who opted out.

    The opt-out set is one query for the whole fan-out. Studio asks per
    recipient, which is O(N) queries to answer a question with N ids in it.
    """
    opted_out = set(
        NotificationSetting.objects.filter(
            user_id__in=[n.user_id for n in notifications],
            email_enabled=False,
        ).values_list("user_id", flat=True)
    )

    workspace_id = getattr(workspace, "pk", workspace)
    for notification in notifications:
        if notification.user_id in opted_out or not notification.user.email:
            continue
        delivery = NotificationDelivery.objects.create(
            notification=notification,
            channel=Channel.EMAIL,
            status=DeliveryStatus.PENDING,
        )
        if queue.enqueue_email(delivery, workspace_id=workspace_id):
            NotificationDelivery.objects.filter(pk=delivery.pk).update(status=DeliveryStatus.QUEUED)
            continue
        # No queue (issue #5 has not merged, or it is not installed). Send after
        # the caller's transaction commits, so a rollback does not leave a sent
        # email behind. Outside a transaction, on_commit runs immediately.
        # partial() rather than a lambda with a default argument: both bind the
        # loop variable correctly, but only one of them is inferrable.
        transaction.on_commit(partial(send_delivery, delivery))
