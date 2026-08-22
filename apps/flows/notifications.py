"""One derived notification event, registered from this app rather than #7's.

``apps/notifications/events.py`` already carries ``member_mentioned`` and its
comment names this consumer: "SPEC §11.2: the action node's notify_members." It
earns an email by default, which is right for ``via: "email"`` and wrong for
``via: "in_app"`` — and :func:`~apps.notifications.engine.notify` has no
per-call override, deliberately: "the product decides which events deserve an
email, the person decides whether they get any email at all."

So the in-app-only variant is a *second registered event*, added from this app's
``ready()`` the way that module's docstring invites ("a later app registers from
its own AppConfig.ready() and never edits this file"). It is built with
``dataclasses.replace`` from the registered original rather than retyped, so the
two cannot drift: rewording ``member_mentioned`` rewords both.
"""

import dataclasses

from apps.notifications.events import get_event, register_event

__all__ = ["EVENT_MEMBER_NOTIFIED_EMAIL", "EVENT_MEMBER_NOTIFIED_IN_APP", "event_for_via"]

#: SPEC §11.2's ``via: "email"`` — the registered event, which emails by default.
EVENT_MEMBER_NOTIFIED_EMAIL = "member_mentioned"

#: SPEC §11.2's ``via: "in_app"`` — same copy, no email.
EVENT_MEMBER_NOTIFIED_IN_APP = "member_mentioned_in_app"


def _register_in_app_variant() -> None:
    source = get_event(EVENT_MEMBER_NOTIFIED_EMAIL)
    if source is None:  # pragma: no cover - the event is registered at import of #7's module
        return
    register_event(dataclasses.replace(source, key=EVENT_MEMBER_NOTIFIED_IN_APP, emails_by_default=False))


_register_in_app_variant()


def event_for_via(via: str) -> str:
    """The event key an action node's ``notify_members`` should use.

    Anything other than ``"email"`` is treated as in-app. The schema's enum has
    only the two values, so this is a default rather than a branch — and
    defaulting to *not* emailing is the right direction to be wrong in.
    """
    return EVENT_MEMBER_NOTIFIED_EMAIL if via == "email" else EVENT_MEMBER_NOTIFIED_IN_APP
