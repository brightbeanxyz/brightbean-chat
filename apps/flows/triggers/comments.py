"""What happens *after* a comment is claimed — SPEC §10's public reply and DM.

``apps.flows.triggers.matching`` already names this module: the comment matcher's
docstring says the once-per-contact-per-post guard is taken by "the routing stage
… through :mod:`apps.flows.triggers.comments`", and
``apps.flows.triggers.stages._claim_comment`` is where the claim is taken. What was
missing until now is the other half — the part that answers the comment — because
answering it is entirely platform work: a public reply is a Graph edge, a like is
a Graph edge, and Meta's private reply is a Send API call addressed by comment id
rather than by person.

**A registry, not a switch**, for the reason ``triggers/hooks.py`` and
``triggers/matching.py`` are: L5-A (Instagram) and L5-B (Messenger) each register
one callable from their own ``ready()``, and neither edits the routing code. A
platform with nothing registered simply leaves the claim standing — the comment
stays recorded, the guard stays spent, and nothing is sent — which is exactly the
behaviour before this module existed.

--------------------------------------------------------------------------
Actions do not run inline
--------------------------------------------------------------------------

An implementation registered here is called from inside the webhook request,
which SPEC §7.1 budgets at 1.5 s of wall clock for *everything*. A public reply, a
like and a private reply are three round trips to Meta, so a platform's callable
is expected to **enqueue** and return, the way
``apps.channels.providers.telegram._answer_callback_query`` does for a spinner.
:func:`run_actions` does not enforce that — it cannot — but it does swallow and
log every failure, because a comment that could not be answered must not take
down the delivery that carried it.

--------------------------------------------------------------------------
The private reply is the flow's first message
--------------------------------------------------------------------------

SPEC §10: "the private reply (the flow's first message) counts against the
one-private-reply-per-comment rule". That is a real constraint rather than a
turn of phrase — Meta allows exactly one message in reply to a comment, and the
standard messaging window only opens once the person answers — so a design that
sent an opener *and then* started a flow would have its second message refused by
the platform.

:func:`pending_private_reply` is how that is honoured without the adapter reaching
into ``apps.flows.models``. The platform's queued worker opens the DM thread and
starts the flow; when the engine's first send reaches the adapter, the adapter
asks this function whether the message it is about to send is still owed as a
private reply, addresses it accordingly, and reports back through
:func:`mark_private_reply_sent`. One question, one answer, no model import across
the app boundary — the same shape ``apps/flows/messaging.py`` uses in the other
direction.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from django.utils import timezone

from apps.flows.models import HandledComment
from apps.flows.triggers.guards import may_private_reply

__all__ = [
    "PRIVATE_REPLY_HANDOFF",
    "ClaimedFlowNotRunnableError",
    "CommentClaim",
    "actions_for",
    "mark_private_reply_sent",
    "pending_private_reply",
    "register_comment_actions",
    "registered_platforms",
    "run_actions",
    "start_claimed_flow",
]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CommentClaim:
    """Everything a platform needs to answer one claimed comment.

    A frozen record rather than four positional arguments, so a later field —
    the notes dict, the matched variables — is added without every registered
    callable changing signature on the same commit.
    """

    #: The ``HandledComment`` row the claim was recorded on. Its id is the
    #: durable handle: a queued action carries the id, never the object.
    row: HandledComment
    #: The connection the comment arrived on.
    connection: Any
    #: The trigger that matched, whose ``config_json`` holds ``public_reply``,
    #: ``like_comment`` and the post scope.
    trigger: Any
    #: The normalised comment event, for its text, ids and timestamp.
    event: Any


Actions = Callable[[CommentClaim], None]

_ACTIONS: dict[str, Actions] = {}


def register_comment_actions(platform: str, actions: Actions, *, replace: bool = False) -> Actions:
    """Register how ``platform`` answers a claimed comment.

    Duplicates raise, the way ``register_adapter`` and ``register_matcher`` do:
    two implementations for one platform is a merge accident, and which one wins
    must not depend on import order.
    """
    existing = _ACTIONS.get(platform)
    if existing is not None and not replace and existing is not actions:
        raise ValueError(
            f"{platform!r} already has comment actions registered "
            f"({existing.__module__}.{getattr(existing, '__qualname__', existing)}). "
            f"Pass replace=True if the override is deliberate."
        )
    _ACTIONS[platform] = actions
    return actions


def actions_for(platform: str) -> Actions | None:
    """The registered callable for ``platform``, or None."""
    return _ACTIONS.get(platform)


def registered_platforms() -> tuple[str, ...]:
    """Platforms that can answer a comment, sorted. For tests and for ops."""
    return tuple(sorted(_ACTIONS))


def run_actions(claim: CommentClaim) -> None:
    """Hand a fresh claim to its platform, if that platform has anything to say.

    Never raises. It is called from ``stages._claim_comment``, which has just
    taken a database guard inside the routing stage: an exception here would be
    caught by ``hooks._run_one`` and reported as a *failed hook*, rolling back the
    savepoint and therefore the claim — so a platform whose API was briefly down
    would silently un-record the comment and let the next redelivery try again,
    which is the one outcome the once-only guard exists to prevent.
    """
    actions = _ACTIONS.get(getattr(claim.connection, "platform", ""))
    if actions is None:
        logger.debug(
            "No comment actions registered for %s; comment %s is claimed but unanswered.",
            getattr(claim.connection, "platform", "?"),
            claim.row.pk,
        )
        return
    try:
        actions(claim)
    except Exception:
        # Nothing platform-supplied reaches the log line: an attacker-controlled
        # id in a log message is a log-injection primitive. The row id is a UUID.
        logger.exception("Comment actions failed for handled comment %s", claim.row.pk)


# ---------------------------------------------------------------------------
# Starting the claimed comment's flow
# ---------------------------------------------------------------------------


class ClaimedFlowNotRunnableError(RuntimeError):
    """The claimed comment's trigger points at a flow that cannot start.

    A configuration problem — a trigger whose flow has no publishable version, or
    whose trigger row has since been deleted — so a caller should log it and stop
    rather than retry. Distinguished from every other failure precisely so the
    ones that *are* worth retrying can propagate.
    """


def start_claimed_flow(row: HandledComment, contact: Any, connection: Any) -> None:
    """Run the flow the claimed comment's trigger points at, for ``contact``.

    Here rather than in the adapter that calls it, which is the point: starting a
    flow is ``apps.flows``' own vocabulary — the ``StartedBy`` stamp, the variables
    a trigger passes, which exception means "configuration problem, do not retry".
    A channels provider that spelled all that out itself would be a second copy of
    ``stages._start``, silently diverging the first time L6-A adds a variable or a
    new non-retryable case.

    Raises :class:`ClaimedFlowNotRunnableError` for the one failure retrying cannot
    fix. Everything else propagates, so a caller running on the queue retries it.
    """
    from apps.flows.engine import FlowNotRunnableError, start_flow
    from apps.flows.models import StartedBy

    trigger = row.trigger
    if trigger is None:
        raise ClaimedFlowNotRunnableError(f"handled comment {row.pk} has no trigger left to run")
    try:
        start_flow(
            contact,
            trigger.flow,
            started_by=StartedBy.stamp(StartedBy.TRIGGER, trigger.pk),
            variables={"trigger_type": trigger.type},
            connection=connection,
        )
    except FlowNotRunnableError as exc:
        raise ClaimedFlowNotRunnableError(str(exc)) from exc


# ---------------------------------------------------------------------------
# The private-reply question, asked by adapters
# ---------------------------------------------------------------------------


#: How long after a claim is recorded its private reply is still offered to a send.
#:
#: **Not** SPEC §10's seven days — that is the platform's outside limit, and it is
#: still enforced by ``guards.may_private_reply``. This is a much shorter
#: hand-off window, and it exists because the adapter cannot see *which* send it
#: is about to make.
#:
#: The claim is answered by whatever message reaches the contact first. Over seven
#: days that is far too loose a net: if the trigger's flow opens with a condition,
#: a delay or an action, or fails to start at all, the claim stays open and the
#: next message of *any* kind — an agent's inbox reply, a broadcast fan-out, an
#: unrelated flow — would be addressed as a reply to a week-old comment, spending
#: the one private reply Meta allows on a message that is not the comment
#: trigger's first. Minutes covers the real case (the worker starts the flow and
#: its first node sends) and excludes that one.
#:
#: Past it the flow's first message goes out as an ordinary DM instead, through
#: the 24-hour window the comment opened — a worse-looking reply, not a failed one.
PRIVATE_REPLY_HANDOFF = timedelta(minutes=10)


def pending_private_reply(connection: Any, commenter_ref: str, *, now: datetime | None = None) -> HandledComment | None:
    """The comment this person is still owed a private reply to, or None.

    Asked by an adapter immediately before a send, so it can address the call as
    Meta's private reply (``recipient={"comment_id": …}``) rather than as an
    ordinary message to a person.

    **Keyed on the platform's own user id, not on a contact.**
    :class:`~apps.flows.models.HandledComment` explains why at length: a comment
    creates no contact, so ``contact`` is NULL at the moment the guard is taken and
    is filled in later — by :func:`mark_private_reply_sent`, which is the call this
    one leads to. A query keyed on the contact would therefore match nothing at
    exactly the moment it is asked.

    **Bounded by :data:`PRIVATE_REPLY_HANDOFF`**, which is the answer to "which
    send is this?" — see that constant.

    Scoped through the connection's workspace like every other tenant read: the
    caller is a worker or a webhook with no session, so the workspace comes from
    the connection rather than from a request. ``may_private_reply`` is re-checked
    per row rather than expressed as a query filter, because the seven-day deadline
    is measured from ``commented_at`` in Python by ``triggers.guards`` — and a
    second spelling of that rule in a ``__gte`` lookup is how a guard and its query
    end up disagreeing about which comments are still answerable.
    """
    if connection is None or not commenter_ref:
        return None
    moment = now or timezone.now()
    rows = (
        HandledComment.objects.for_workspace(connection.workspace_id)
        .filter(
            channel_connection=connection,
            commenter_ref=commenter_ref,
            private_reply_sent_at__isnull=True,
            created_at__gte=moment - PRIVATE_REPLY_HANDOFF,
        )
        # Newest first. Every row here is inside the hand-off window, so they are
        # all answerable; the most recent comment is the one this send is most
        # likely to be about. Ordering oldest-first with a fixed slice used to be
        # able to hide an answerable claim behind expired ones.
        .order_by("-commented_at")
    )
    for row in rows:
        if may_private_reply(row, now=moment):
            return row
    return None


def mark_private_reply_sent(row: HandledComment, *, contact: Any = None, now: datetime | None = None) -> None:
    """Record that the private reply went out. Re-exported from ``guards``.

    Here as well as there so an adapter has **one** module to import from this
    package — it asks :func:`pending_private_reply` and answers with this, rather
    than importing a guard for one half and a registry for the other.
    """
    from apps.flows.triggers.guards import mark_private_reply_sent as _mark

    _mark(row, contact=contact, now=now)
