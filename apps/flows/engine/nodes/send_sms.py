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

        connection = _sms_target(ctx)
        if connection is None:
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


def _sms_target(ctx: NodeContext) -> Any:
    """The SMS connection to send this contact's message on, or ``None``.

    **Resolved together with the identity, not before it.** Picking a connection
    first and then asking whether the contact has a number on it was wrong the
    moment a workspace ran two numbers: a contact who has only ever texted the
    *newer* one has their identity attached to that connection alone, so
    choosing the oldest and looking there found nothing and followed the
    ``error`` edge — with a perfectly good active connection and phone identity
    sitting in the same workspace. The question the node actually has is "where
    can I reach this person", and that is one query over identities, not two
    queries that have to agree.

    Preference order, and each step is a decision rather than a tie-break:

    1. an identity already bound to an **active** SMS connection — that is a
       number the contact has demonstrably used, and its connection is the one
       the send should go out on;
    2. failing that, a *pending* identity (captured before any SMS connection
       existed, so ``channel_connection`` is NULL) sent on the workspace's
       oldest active connection — the same connection
       ``services.upsert_contact_identity`` would have bound it to, so the
       facade's own lazy upgrade lands where this node predicted;
    3. failing that, nothing, and the caller takes the ``error`` handle.

    Among several bound identities the oldest connection wins, so a contact who
    has used both numbers gets a stable answer rather than one that depends on
    row order.

    This still duplicates a lookup ``send_outbound`` makes, and the extra query
    buys the same thing it did before: the facade answers "no identity" by
    opening a conversation and writing a ``failed`` row into it, which for a
    flow that texts whoever it can would file an empty SMS thread in the inbox
    for every contact without a number.
    """
    from django.db.models import F, Q

    from apps.channels.models import ChannelConnection, ConnectionStatus

    connections = list(
        ChannelConnection.objects.for_workspace(ctx.workspace_id)
        .filter(platform=Platform.SMS.value, status=ConnectionStatus.ACTIVE)
        .order_by("created_at")
    )
    if not connections:
        logger.info("Execution %s: node %s found no SMS channel in this workspace.", ctx.execution.pk, ctx.node_id)
        return None

    model = installed_model("messaging", "apps.messaging", "ContactChannelIdentity")
    if model is None:  # pragma: no cover - messaging is installed in every deployment
        return None

    identity = (
        model.objects.for_workspace(ctx.workspace_id)
        .filter(Q(channel_connection__in=connections) | Q(channel_connection__isnull=True))
        .filter(contact=ctx.contact, platform=Platform.SMS.value)
        # A bound identity beats a pending one — ``nulls_last`` says so rather
        # than leaving it to Postgres' default ordering for NULLs — and among
        # bound ones the oldest connection wins.
        .order_by(F("channel_connection__created_at").asc(nulls_last=True), "created_at")
        .select_related("channel_connection")
        .first()
    )
    if identity is None:
        logger.info("Execution %s: node %s found no phone identity for this contact.", ctx.execution.pk, ctx.node_id)
        return None

    # A bound identity names its own connection; a pending one is upgraded by
    # the facade at first send, onto the connection chosen here.
    return identity.channel_connection or connections[0]
