"""The in-app notification a finished broadcast produces (issue #7's engine).

Registered from this app's ``ready()`` rather than added to
``apps/notifications/events.py``, which is what that module's docstring invites:
"a later app registers from its own AppConfig.ready() and never edits this file".

``emails_by_default=False``. ``NotificationEvent``'s own rule is that an event
nobody can act on from their inbox does not earn an email — and the answer to
"your broadcast finished" is to open the page and read the counters, which is
one click from the bell and several from a mailbox.
"""

from apps.notifications.events import NotificationEvent, register_event

__all__ = ["EVENT_BROADCAST_FINISHED", "register"]

#: The notification event key. Deliberately **not** the catalog event name
#: (``broadcast.finished``, :mod:`apps.broadcasts.events`): one is a webhook wire
#: format an operator has stored in a subscription row, the other is copy in
#: somebody's bell. Conflating them would tie a reword of the copy to a break in
#: configured integrations.
EVENT_BROADCAST_FINISHED = "broadcast_finished"


def register() -> None:
    """Add the event type. Re-registering the identical event is a no-op."""
    register_event(
        NotificationEvent(
            key=EVENT_BROADCAST_FINISHED,
            label="Broadcast finished",
            icon="broadcasts",
            tone="success",
            title='Broadcast "{broadcast_name}" finished',
            body="{sent} sent, {failed} failed, {skipped} skipped.",
            required_context=("broadcast_name",),
            emails_by_default=False,
        )
    )


register()
