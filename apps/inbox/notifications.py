"""The inbox's notification events, registered from this app rather than #7's.

``apps/notifications/events.py`` already carries ``inbox_reminder`` — its comment
reads "SPEC §14: a scheduled_action that becomes an in-app notification", and it
was registered ahead of this issue with ``emails_by_default=False`` because an
in-app reminder you set for yourself is the whole point of one. So this module
only adds the event that module could not have anticipated: what a scheduled
reply says when it fails.

Registered from here the way that module invites — "a later app registers from
its own AppConfig.ready() and never edits this file" — and ``register_event`` is
a no-op for an identical re-registration, so an autoreloaded ``ready()`` is
harmless.
"""

from apps.notifications.events import NotificationEvent, register_event

__all__ = ["EVENT_REMINDER", "EVENT_SCHEDULED_REPLY_FAILED"]

#: Already registered by issue #7. Named here so the handler spells it once.
EVENT_REMINDER = "inbox_reminder"

#: A reply an agent queued that the compliance engine refused when it came due.
EVENT_SCHEDULED_REPLY_FAILED = "scheduled_reply_failed"


register_event(
    NotificationEvent(
        key=EVENT_SCHEDULED_REPLY_FAILED,
        label="Scheduled reply failed",
        icon="inbox",
        tone="error",
        title="Scheduled reply to {contact_name} was not sent",
        body="{reason}",
        # This one **does** earn an email, unlike the reminder beside it. The
        # test ``apps/notifications/events.py`` sets is "can the recipient act on
        # it from their inbox, and can it fire in a burst" — and a refused reply
        # is a single event about a message the agent believed had gone out
        # hours ago. Finding out at their next login is finding out too late.
        emails_by_default=True,
        email_subject="A scheduled reply was not sent",
        # Without the reason the notification says a send failed and nothing
        # about why, which is the sentence-with-a-hole this field exists to stop.
        required_context=("reason",),
    )
)
