"""Machine-readable failure codes this app raises on its own.

``apps.messaging.codes.describe`` is the vocabulary for anything the *send*
pipeline decided — a compliance denial, a provider rejection — and it returns
the code itself for a string it does not recognise, which is the right default
there and the wrong thing to print in a thread.

So the one failure the inbox reaches on its own gets its sentence here rather
than by widening another app's table for a case that app cannot cause.
:func:`describe_inbox_failure` falls through to ``describe`` for everything else,
so callers need only one function.
"""

from django.utils.functional import Promise
from django.utils.translation import gettext_lazy as _

from apps.messaging.codes import describe

__all__ = ["EMPTY_BODY", "describe_inbox_failure"]

#: A scheduled reply whose stored body carries no renderable block. Reachable
#: only through a hand-edited row or a body written by an older release, and
#: terminal either way — no number of retries gives it something to send.
EMPTY_BODY = "empty_body"

_COPY: dict[str, str | Promise] = {
    EMPTY_BODY: _("That scheduled reply had nothing left to send."),
}


def describe_inbox_failure(code: str) -> str | Promise:
    """The sentence for ``code``, from this app's table or messaging's.

    Stays lazy when this table has an entry, rather than resolving with
    ``str()``. A scheduled reply's failure handler (``apps.inbox.handlers._fail``)
    calls this in a queue worker, before the notification it feeds ever reaches
    ``apps.notifications.engine.notify``'s per-recipient ``translation.override``
    — resolving here would freeze the sentence in whichever language happened to
    be active in the worker process, not the recipient's. ``str.format_map``
    resolves a lazy value itself, fresh, at the point a notification's title/body
    template is filled — see ``apps.notifications.events._format``.
    """
    copy = _COPY.get(code)
    return copy if copy else describe(code)
