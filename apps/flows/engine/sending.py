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
"""

import logging
from dataclasses import dataclass, replace
from typing import Any

from apps.channels.events import OutboundMessage, TextBlock
from apps.flows import messaging
from apps.flows.models import FlowExecution

__all__ = ["SEND_SOURCE", "SendOutcome", "deliver", "text_message"]

logger = logging.getLogger(__name__)

#: SPEC §5's ``message.source`` for anything a flow sends. Not ``agent`` — that
#: value carries the 30-minute automation pause and the human-agent tag window,
#: neither of which an automation may claim.
SEND_SOURCE = "automation"


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

    message = messaging.send_outbound(
        workspace=execution.workspace,
        contact=execution.contact,
        connection=execution.channel_connection,
        # Stamped here rather than by each node, because this is the one place
        # that knows both the message and the node it came from. SPEC §6.2 needs
        # it in Telegram's `callback_data` as `node_id:button_id`, and Meta's
        # postback payloads take the same shape (issue #12). Adapters that have
        # no use for it ignore it.
        outbound=replace(outbound, node_id=node_id),
        source=SEND_SOURCE,
        idempotency_key=messaging.message_idempotency_key(execution, node_id, attempt),
    )
    status = str(getattr(message, "status", "") or "")
    error = str(getattr(message, "error", "") or "")
    if status == "failed":
        # SPEC §9.5: the message failed, the flow does not. The caller follows
        # `default` onward; the reason is on the message row for the inbox.
        logger.info("Execution %s node %s: send failed (%s)", execution.pk, node_id, error or "no reason given")
        return SendOutcome(sent=False, message=message, error=error)
    return SendOutcome(sent=True, message=message)
