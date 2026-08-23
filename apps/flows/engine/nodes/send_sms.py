"""SPEC §11.9 — send one SMS, on the SMS channel, whatever channel the run is on.

    Config: text, media_url optional. Requires an SMS connection and contact
    phone identity; missing -> follow error handle. Handles: default, error.
    Compliance engine applies (opt-out suppression).

The sentence that makes this node different from ``send_message`` is "requires an
SMS connection": every other sending node delivers on
``execution.channel_connection``, the channel the conversation is happening on.
This one crosses. A contact who started a flow in a Telegram chat and reaches a
``send_sms`` gets a text message, and the two facts the node needs — which SMS
connection, and this contact's number on it — are neither of them on the
execution.

That is also why it cannot go through :func:`apps.flows.engine.sending.deliver`,
which exists to be the one place a node hands a message to contract 1 and takes
``execution.channel_connection`` as given. Everything else about the send is the
same and comes from the same facade: the SPEC §9.4 idempotency key, the
compliance verdict, the message row, the token bucket.

**Compliance is not bypassed and cannot be.** ``source="automation"`` goes
through ``can_send`` like everything else, so a contact who texted STOP gets a
``failed`` row with ``opted_out`` and the flow follows its edge onward — SPEC §19
puts that at a chokepoint precisely so a node cannot opt out of it.

**Never inline.** ``synchronous_safe = False``, which the issue specifies and
which L4-A's budget reads off the class through
:func:`apps.flows.engine.registry.synchronous_safe` — there is deliberately no
second list of safe node types anywhere. The reason is the crossing: reaching
this node costs two queries the inline path has not already paid (the workspace's
SMS connection, then the identity on it) before a Twilio round trip that has
nothing to do with the webhook currently being answered.

**Nothing here is a ``Fail``.** SPEC §11.9 routes both missing prerequisites to
the ``error`` handle, and SPEC §9.5 routes a failed send onward — a text message
that could not be sent is a result the author can branch on, not a broken run.
"""

import logging
from typing import Any

from apps.channels.events import MediaBlock, OutboundMessage, TextBlock
from apps.common.platforms import Platform
from apps.flows import messaging
from apps.flows.compat import installed_model
from apps.flows.engine.context import NodeContext
from apps.flows.engine.nodes.base import Node
from apps.flows.engine.registry import register_node
from apps.flows.engine.results import Continue, Fail, StepResult
from apps.flows.engine.sending import SEND_SOURCE
from apps.flows.messaging import FacadeUnavailableError

__all__ = ["SendSmsNode"]

logger = logging.getLogger(__name__)


@register_node
class SendSmsNode(Node):
    """Send an SMS to this contact's phone identity (SPEC §11.9)."""

    type = "send_sms"
    synchronous_safe = False

    def execute(self, ctx: NodeContext) -> StepResult:
        # Stripped, because whitespace is not a text message. A body of spaces
        # is either a draft or a placeholder that rendered to nothing, and
        # Twilio would charge a segment for delivering it.
        text = ctx.render(ctx.config.get("text")).strip()
        # ``mode="url"`` percent-encodes each *substituted value*, never the
        # template, so ``https://cdn.example/{{first_name}}.jpg`` keeps its shape
        # while a contact called ``../private/secret`` becomes a path segment
        # rather than a path traversal. Twilio fetches this URL server-side, so
        # the encoding is the difference between a placeholder and an injection
        # point (SECURITY-BASELINE §3); ``external_request`` renders its URL the
        # same way and for the same reason.
        media_url = ctx.render(ctx.config.get("media_url"), mode="url").strip()
        if not text and not media_url:
            # The schema requires ``text``, so this is a draft, a hand-edited
            # graph, or a placeholder that rendered empty. The error handle is
            # where a node that cannot send belongs.
            logger.warning("Execution %s: node %s has no SMS body.", ctx.execution.pk, ctx.node_id)
            return Continue("error")

        connection = _sms_connection(ctx)
        if connection is None:
            logger.info("Execution %s: node %s found no SMS channel in this workspace.", ctx.execution.pk, ctx.node_id)
            return Continue("error")

        identity = _sms_identity(ctx, connection)
        if identity is None:
            logger.info(
                "Execution %s: node %s found no phone identity for this contact.", ctx.execution.pk, ctx.node_id
            )
            return Continue("error")

        blocks: list[Any] = []
        if text:
            blocks.append(TextBlock(text=text))
        if media_url:
            # ``image`` because that is the only media kind SMS declares
            # (``capabilities_for(Platform.SMS)``); anything else would be
            # downgraded to a link in the text by the shared renderer anyway.
            blocks.append(MediaBlock(kind="image", url=media_url))

        try:
            message = messaging.send_outbound(
                workspace=ctx.workspace,
                contact=ctx.contact,
                connection=connection,
                outbound=OutboundMessage(blocks=tuple(blocks), node_id=ctx.node_id),
                source=SEND_SOURCE,
                idempotency_key=messaging.message_idempotency_key(ctx.execution, ctx.node_id),
            )
        except FacadeUnavailableError as exc:
            # A deployment problem rather than a flow problem, and one no retry
            # fixes — the same call ``send_message`` makes.
            return Fail(f"send_sms node {ctx.node_id}: {exc}")

        if str(getattr(message, "status", "") or "") == "failed":
            # Suppression lands here: an opted-out contact comes back as a
            # ``failed`` row carrying ``opted_out``, and the reason is on the row
            # for the inbox rather than in this log line.
            logger.info(
                "Execution %s node %s: SMS not sent (%s)",
                ctx.execution.pk,
                ctx.node_id,
                str(getattr(message, "error", "") or "") or "no reason given",
            )
            return Continue("error")
        return Continue("default")


def _sms_connection(ctx: NodeContext) -> Any:
    """The workspace's SMS channel, oldest active first, or ``None``.

    Oldest-first rather than "the one the run is on", because the run is very
    likely not on an SMS connection at all — and it is the same tie-break
    ``apps.messaging.services.upsert_contact_identity`` uses when a workspace
    runs more than one number, so the connection this node picks is the one that
    call would have attached an identity to.

    Imported inside the function, matching ``apps.flows.handlers._connection``:
    every other engine module reaches ``apps.channels`` for its *data* tables
    (capabilities, the event schema) and none of them import its models, so
    keeping the model import local keeps that shape visible.
    """
    from apps.channels.models import ChannelConnection, ConnectionStatus

    return (
        ChannelConnection.objects.for_workspace(ctx.workspace_id)
        .filter(platform=Platform.SMS.value, status=ConnectionStatus.ACTIVE)
        .order_by("created_at")
        .first()
    )


def _sms_identity(ctx: NodeContext, connection: Any) -> Any:
    """This contact's number for ``connection``, or ``None``.

    A **read**, deliberately: it never creates an identity from
    ``contact.phone``. A number typed into a CRM field is not consent to text it
    — SPEC §11.8 makes ``opt_in_source`` part of the audit and this node has
    nothing truthful to put there — and fabricating one would route straight
    past the compliance engine's ``no_opt_in`` rule. A contact with no SMS
    identity follows the ``error`` handle, which is what SPEC §11.9 asks for.

    **This duplicates a check ``send_outbound`` also makes**, and the extra query
    buys something specific: the facade answers "no identity" by opening a
    conversation and writing a ``failed`` message row into it. For a node that
    can legitimately run against contacts who have never given a phone number —
    a flow that texts whoever it can and carries on — that would file an empty
    SMS thread in the inbox for every one of them. Checking first keeps the
    ``error`` handle free of that side effect. The facade's own check stays as
    the authority; this is a pre-filter, not a second opinion.

    A *pending* identity — captured before any SMS connection existed, so
    ``channel_connection`` is NULL — counts, because contract 1 upgrades exactly
    those at first send. It is preferred **last**: a row already attached to this
    connection is the one the send will use, and ``nulls_last`` says so rather
    than relying on Postgres' default ordering for NULLs.

    Reached through :func:`apps.flows.compat.installed_model` rather than an
    import, unlike the connection above: ``apps.flows`` never imports
    ``apps.messaging`` at module scope — that is the whole point of contract 1's
    seam in :mod:`apps.flows.messaging` — and this is the same lookup
    ``apps/flows/triggers/context.py`` makes.
    """
    from django.db.models import F, Q

    model = installed_model("messaging", "apps.messaging", "ContactChannelIdentity")
    if model is None:  # pragma: no cover - messaging is installed in every deployment
        return None
    return (
        model.objects.for_workspace(ctx.workspace_id)
        .filter(Q(channel_connection=connection) | Q(channel_connection__isnull=True))
        .filter(contact=ctx.contact, platform=Platform.SMS.value)
        .order_by(F("channel_connection_id").asc(nulls_last=True), "created_at")
        .first()
    )
