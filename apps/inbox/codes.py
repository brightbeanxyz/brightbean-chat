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

from apps.messaging.codes import describe

__all__ = ["EMPTY_BODY", "describe_inbox_failure"]

#: A scheduled reply whose stored body carries no renderable block. Reachable
#: only through a hand-edited row or a body written by an older release, and
#: terminal either way — no number of retries gives it something to send.
EMPTY_BODY = "empty_body"

_COPY: dict[str, str] = {
    EMPTY_BODY: "That scheduled reply had nothing left to send.",
}


def describe_inbox_failure(code: str) -> str:
    """The sentence for ``code``, from this app's table or messaging's."""
    return _COPY.get(code) or describe(code)
