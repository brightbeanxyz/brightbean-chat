"""The one place a node hands a message to ROADMAP contract 1.

Three nodes and one resume path send messages — ``send_message``,
``data_collection``'s question, the retry text on an unmatched reply — and all
four need the same four things right: the idempotency key SPEC §9.4 specifies,
the connection SPEC §9.3 says the run is happening on, the source string
compliance keys off, and SPEC §9.5's rule about what a failed send means.

    Node-level provider errors: 4xx permanent -> message failed, follow
    ``default`` edge onward (sending failure does not kill the flow) […]

So the send lives here rather than in the send node, and the other three call
into it instead of reimplementing three quarters of it.

**A failed send is not a failed run.** :func:`deliver` returns the outcome; it
never raises for a compliance denial or a provider 4xx, because contract 1 is
explicit that those come back as a ``Message`` with ``status="failed"`` and a
machine-readable code. What *does* propagate is the facade being absent (a
deployment problem, turned into a named ``Fail`` by the caller) and anything
unexpected (rolled back and retried by the queue).

--------------------------------------------------------------------------
The envelope: what the run's *starter* contributes to the send
--------------------------------------------------------------------------

Two fields of an outbound message are decided by whoever started the run rather
than by the flow author, and neither can be reconstructed from a node's config.

``source``. SPEC §5 fixes the vocabulary at ``automation, agent, api, broadcast,
sequence``, and ``started_by`` is the only thing that knows which of the last two
a run is: a broadcast's one-shot flow start stamps ``broadcast:<id>``, a
sequence step ``sequence:<id>``. Recording every one of them as ``automation``
would put a broadcast's messages in the inbox under the wrong label and hide them
from L7-A's counters, and it would bypass ``PlatformPolicy.broadcast_allowed``,
which SPEC §8 gates on exactly this value.

``tag``. SPEC §6.4 lets a Messenger send outside the 24-hour window carry a
non-promotional message tag, and SPEC §13.1 makes choosing one the broadcast
composer's job. The compliance engine already reads ``outbound.tag`` — its
``_outside_window_code`` grants ``tag_supplied`` for a tag in the platform's own
``PlatformPolicy.outside_window.tags`` — so the only piece missing was a way for
the starter to supply one. It travels in :data:`ENVELOPE_TAG_VAR`, a reserved
execution variable.

**Why that is not a hole.** The envelope is read only for a run whose
``started_by`` kind is ``broadcast``, so a variable a flow author collects — a
``data_collection`` field, an External Request mapping — can never reach it. And
the tag is still policy-validated downstream: ``HUMAN_AGENT`` is not in
Messenger's tag tuple, and ``can_send`` grants the human-agent allowance only to
``source="agent"`` (SPEC §22, hard-coded), so nothing here buys the seven-day
escape.
"""

import logging
from dataclasses import dataclass, replace
from typing import Any

from apps.channels.events import OutboundMessage, TextBlock
from apps.flows import analytics, messaging
from apps.flows.models import FlowExecution, StartedBy

__all__ = [
    "ENVELOPE_TAG_VAR",
    "SEND_SOURCE",
    "STARTER_SOURCES",
    "Envelope",
    "SendOutcome",
    "deliver",
    "envelope_for",
    "text_message",
]

logger = logging.getLogger(__name__)

#: SPEC §5's ``message.source`` for anything a flow sends. Not ``agent`` — that
#: value carries the 30-minute automation pause and the human-agent tag window,
#: neither of which an automation may claim.
SEND_SOURCE = "automation"

#: ``started_by`` kind -> the SPEC §5 ``message.source`` it implies. Only the two
#: kinds that name a *campaign* are here: a trigger, the API, a manual start and
#: a preview are all ordinary automation as far as compliance and the inbox are
#: concerned, and SPEC §5's vocabulary has no word for any of them.
STARTER_SOURCES: dict[str, str] = {
    StartedBy.BROADCAST: "broadcast",
    StartedBy.SEQUENCE: "sequence",
}

#: The execution variable a broadcast puts its compliance tag in (SPEC §6.4).
#:
#: A reserved name, marked by the leading dunder, and read **only** for a run
#: started by a broadcast — see the module docstring for why that gate is the
#: security property rather than the name.
ENVELOPE_TAG_VAR = "__message_tag"

#: Which starters may set a tag. Just the one: a broadcast is the only thing in
#: the product that chooses a message tag, because it is the only thing with a
#: composer that shows Meta's allowed-use text next to the choice.
_TAG_STARTERS = frozenset({StartedBy.BROADCAST})


@dataclass(frozen=True)
class Envelope:
    """The two send fields a run's starter decides. See the module docstring."""

    source: str = SEND_SOURCE
    tag: str | None = None


def envelope_for(execution: FlowExecution) -> Envelope:
    """What ``started_by`` says about how this run's messages should go out.

    ``started_by`` is written by ``StartedBy.stamp`` as ``kind`` or ``kind:id``,
    so the kind is everything up to the first colon and there is no second column
    to keep in step.
    """
    kind = str(execution.started_by or "").partition(":")[0]
    source = STARTER_SOURCES.get(kind, SEND_SOURCE)
    if kind not in _TAG_STARTERS:
        return Envelope(source=source)
    variables = execution.variables if isinstance(execution.variables, dict) else {}
    tag = variables.get(ENVELOPE_TAG_VAR)
    return Envelope(source=source, tag=str(tag) if tag else None)


@dataclass(frozen=True)
class SendOutcome:
    """What came back, reduced to what a node has to decide on.

    ``sent`` is the only question SPEC §9.5 makes a node ask. The ``message`` is
    carried along for a caller that wants the provider id or the error code, and
    is ``None`` when the facade is not installed.
    """

    sent: bool
    message: Any = None
    error: str = ""


def text_message(text: str) -> OutboundMessage:
    """A one-block plain-text message — retries and questions are always this."""
    return OutboundMessage(blocks=(TextBlock(text=text),))


def deliver(
    execution: FlowExecution,
    outbound: OutboundMessage,
    *,
    node_id: str,
    attempt: int = 0,
) -> SendOutcome:
    """Send ``outbound`` for this execution through the messaging facade.

    ``attempt`` is SPEC §9.4's attempt bucket. It is 0 for everything this layer
    sends inline; the retry paths pass their retry counter so a second prompt is
    a distinct message rather than a duplicate of the first.
    """
    if execution.channel_connection_id is None:
        # Nothing to send on. A run started by the API or a rule trigger has no
        # channel until something gives it one, and inventing one here — "the
        # contact's most recent identity" — would be the send path guessing at
        # routing, which is L3-A's and L4-A's to decide.
        logger.warning("Execution %s has no channel connection; node %s cannot send.", execution.pk, node_id)
        return SendOutcome(sent=False, error="no_connection")

    envelope = envelope_for(execution)
    idempotency_key = messaging.message_idempotency_key(execution, node_id, attempt)
    # Click tracking (issue #26). Wraps URL buttons — on every platform, and
    # including the ones inside a card — before the body is persisted, so a retry
    # rebuilt from the stored row carries the same links as the first attempt.
    # Returns the message untouched for a preview run, which is what keeps a test
    # send out of L7-A's counters.
    outbound = analytics.instrument(
        outbound,
        execution=execution,
        node_id=node_id,
        # getattr rather than a direct attribute read: the guard above is on
        # ``channel_connection_id`` — deliberately, so it costs no query — which
        # leaves the related object typed as optional even though it cannot be
        # None here.
        platform=str(getattr(execution.channel_connection, "platform", "")),
        idempotency_key=idempotency_key,
    )
    message = messaging.send_outbound(
        workspace=execution.workspace,
        contact=execution.contact,
        connection=execution.channel_connection,
        # Stamped here rather than by each node, because this is the one place
        # that knows both the message and the node it came from. SPEC §6.2 needs
        # it in Telegram's `callback_data` as `node_id:button_id`, and Meta's
        # postback payloads take the same shape (issue #12). Adapters that have
        # no use for it ignore it.
        #
        # The envelope's tag is applied the same way and for the same reason: the
        # node knows the content, the starter knows the compliance context, and
        # this is the one place that has both. A node that set its own tag would
        # still lose to it, which is the direction to be wrong in — a tag is a
        # promise about *why* a message is being sent, and only the starter knows.
        outbound=replace(outbound, node_id=node_id, tag=envelope.tag or outbound.tag),
        source=envelope.source,
        idempotency_key=idempotency_key,
    )
    status = str(getattr(message, "status", "") or "")
    error = str(getattr(message, "error", "") or "")
    if status == "failed":
        # SPEC §9.5: the message failed, the flow does not. The caller follows
        # `default` onward; the reason is on the message row for the inbox.
        logger.info("Execution %s node %s: send failed (%s)", execution.pk, node_id, error or "no reason given")
        return SendOutcome(sent=False, message=message, error=error)
    return SendOutcome(sent=True, message=message)
