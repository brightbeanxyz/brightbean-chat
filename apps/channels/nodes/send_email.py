"""SPEC §11.10 — the ``send_email`` node.

    Config: subject, html_body, from override optional. Requires email
    connection + contact email identity, suppression applied. Handles: default,
    error.

The schema for this node has been in ``apps/flows/schema/nodes.py`` since L2-D
(contract 2 ships every node's schema up front so the builder can draw it), so
this module adds the runtime and nothing else.

--------------------------------------------------------------------------
Why it does not use ``sending.deliver``
--------------------------------------------------------------------------

``apps.flows.engine.sending.deliver`` is "the one place a node hands a message to
contract 1", and every other sending node uses it. It sends on
``execution.channel_connection`` — the channel the run is happening on — which is
right for ``send_message`` and wrong here: a ``send_email`` node fires inside a
run that started on Telegram, or on no channel at all, and the email has to go
out over the workspace's *email* connection regardless.

So this node resolves its own connection and calls the facade directly. It still
goes through contract 1 — ``messaging.send_outbound``, compliance, the message
row, the idempotency key from ``messaging.message_idempotency_key`` — because
that is the part that must not be re-implemented. Only the choice of connection
differs, and it differs for a reason SPEC §11.10 states.

--------------------------------------------------------------------------
``error`` versus ``default``
--------------------------------------------------------------------------

The two handles mean different things and the distinction is SPEC's:

``error``
    A **missing prerequisite**. SPEC §11.9 spells it out for the sibling node —
    "Requires an SMS connection and a phone identity; missing -> follow error
    handle" — and §11.10 says the same for this one. The flow author wired an
    email node into a workspace with no email channel, or for a contact whose
    address nobody ever collected; that is a modelling problem they can branch on.

``default``
    A **failed send**. SPEC §9.5: "the message failed, the flow does not". The
    reason is on the message row for the inbox, and the run carries on — which is
    the same answer ``send_message`` gives, and is why an opt-out does not divert
    a flow down its error branch.

--------------------------------------------------------------------------
Consent
--------------------------------------------------------------------------

When the contact has an ``email`` on their record but no email identity, this
node creates one through the facade — and creates it **without opt-in**.

That is deliberate and it is ``apps/contacts/imports.py``'s decision, not a new
one: that module refuses to fabricate identities for imported contacts because
"a spreadsheet column is not consent". Minting one here with ``opt_in=True``
would launder exactly the data it declined to launder, one app over. So the
address is recorded, with its audit trail, and compliance answers ``no_opt_in``
until consent arrives from the contact — through ``data_collection``, which
captures it with ``opt_in=True`` because there the contact typed it themselves.
"""

import logging
from typing import Any

from django.db import IntegrityError, transaction
from django.db.models import Q

from apps.channels.capabilities import capabilities_for
from apps.channels.events import OutboundMessage, TextBlock
from apps.channels.providers import email_html
from apps.common.addresses import normalize_email
from apps.common.platforms import Platform
from apps.flows import analytics, messaging
from apps.flows.engine.context import NodeContext
from apps.flows.engine.nodes.base import Node
from apps.flows.engine.registry import register_node
from apps.flows.engine.results import Continue, Fail, StepResult
from apps.flows.messaging import FacadeUnavailableError

__all__ = ["SendEmailNode"]

logger = logging.getLogger(__name__)

#: SPEC §5's ``message.source`` for anything a flow sends. The same value
#: ``apps.flows.engine.sending.SEND_SOURCE`` uses, and deliberately not
#: ``agent`` — that carries the automation pause and the human-agent allowance,
#: neither of which an automation may claim.
SEND_SOURCE = "automation"

#: How much rendered HTML a body may carry. The node schema caps ``html_body`` at
#: this and so does email's ``Capabilities.max_text_len``; passing it explicitly
#: is what stops the renderer's own 20 000-character default truncating a long
#: but legal email mid-tag.
MAX_BODY_CHARS = capabilities_for(Platform.EMAIL).max_text_len

#: The consent source recorded for an address taken off ``contact.email``. Not
#: ``import`` even when that is where the address came from: this row is being
#: created by an automation, and ``manual`` is the honest label for "somebody at
#: this workspace put this address here". See the module docstring.
CAPTURE_SOURCE = "manual"

#: The longest address that survives a round trip through the identity table.
#: ``apps.messaging.identities.MAX_PLATFORM_USER_ID``, mirrored rather than
#: imported for the same reason ``apps/contacts/imports.py`` mirrors the tag
#: width: this module has to refuse what that one would mangle.
MAX_ADDRESS_CHARS = 200


@register_node
class SendEmailNode(Node):
    """Send one email to the contact's address."""

    type = "send_email"
    #: SPEC §7.1 lists the synchronous-safe five and this is not among them. An
    #: SMTP conversation with a third-party relay is seconds of wall clock, well
    #: outside the 1.5 s inline budget, so it is always enqueued.
    synchronous_safe = False

    def execute(self, ctx: NodeContext) -> StepResult:
        connection = _email_connection(ctx)
        if connection is None:
            logger.info(
                "Execution %s: node %s needs an email connection and this workspace has none.",
                ctx.execution.pk,
                ctx.node_id,
            )
            return Continue("error")

        try:
            identity = _ensure_identity(ctx, connection)
        except FacadeUnavailableError as exc:
            # A deployment problem rather than a flow problem, and no retry
            # fixes it — the same call ``send_message`` makes.
            return Fail(f"send_email node {ctx.node_id}: {exc}")
        if identity is None:
            logger.info(
                "Execution %s: node %s has no email address for this contact.",
                ctx.execution.pk,
                ctx.node_id,
            )
            return Continue("error")

        subject = ctx.render(ctx.config.get("subject"))
        # mode="html" escapes each substituted value and leaves the author's
        # markup alone (SECURITY-BASELINE §3). A contact called `<script>`
        # reaches the mail client as text; the author's `<p>` stays a `<p>`.
        body = ctx.render(ctx.config.get("html_body"), mode="html", max_chars=MAX_BODY_CHARS)
        if not subject or not body:
            # The schema requires both, so this is a draft or a hand-edited
            # graph. Skipping is the quiet equivalent of a failed send, and it
            # follows `default` for the same reason one does.
            logger.warning("Execution %s: node %s has no subject or no body.", ctx.execution.pk, ctx.node_id)
            return Continue("default")

        outbound = OutboundMessage(
            # The HTML goes in `html_body`, and the *plain-text* rendering of it
            # in the block. The blocks are what the inbox thread shows, and raw
            # markup there would be both unreadable and the wrong thing to hand
            # a renderer that treats block text as plain text everywhere else.
            blocks=(TextBlock(text=email_html.to_plain_text(body)),),
            html_body=body,
            subject=subject,
            from_override=normalize_email(ctx.render(ctx.config.get("from_override"))),
            node_id=ctx.node_id,
        )

        # Click and open tracking (issue #26). The same call `sending.deliver`
        # makes for every other node, repeated here for the reason this whole
        # module exists: this node does not go through `deliver`. Both email
        # rewrites are opt-in per workspace and a preview run is left untouched;
        # see apps.analytics.tracking.
        idempotency_key = messaging.message_idempotency_key(ctx.execution, ctx.node_id)
        outbound = analytics.instrument(
            outbound,
            execution=ctx.execution,
            node_id=ctx.node_id,
            platform=Platform.EMAIL.value,
            idempotency_key=idempotency_key,
        )

        try:
            message = messaging.send_outbound(
                workspace=ctx.workspace,
                contact=ctx.contact,
                connection=connection,
                outbound=outbound,
                source=SEND_SOURCE,
                idempotency_key=idempotency_key,
            )
        except FacadeUnavailableError as exc:
            return Fail(f"send_email node {ctx.node_id}: {exc}")

        status = str(getattr(message, "status", "") or "")
        if status == "failed":
            # SPEC §9.5. The reason — `opted_out`, `no_opt_in`, a provider code —
            # is on the row for the inbox; the run continues down `default`.
            logger.info(
                "Execution %s node %s: email not sent (%s)",
                ctx.execution.pk,
                ctx.node_id,
                getattr(message, "error", "") or "no reason given",
            )
        return Continue("default")


def _email_connection(ctx: NodeContext) -> Any:
    """The workspace's email connection, oldest active first.

    Oldest rather than "the run's": see the module docstring. Matching
    ``apps.messaging.services._active_connections``' ordering means the identity
    the facade attaches an address to and the connection this node sends over are
    the same row, which is what keeps a freshly captured address sendable.
    """
    from apps.channels.models import ChannelConnection, ConnectionStatus

    return (
        ChannelConnection.objects.for_workspace(ctx.workspace_id)
        .filter(platform=Platform.EMAIL.value, status=ConnectionStatus.ACTIVE)
        .order_by("created_at")
        .first()
    )


def _ensure_identity(ctx: NodeContext, connection: Any) -> Any:
    """The contact's email identity, creating one from ``contact.email`` if needed.

    Returns ``None`` when the contact has no usable address at all, which is the
    ``error`` handle's case. Creating one is done through contract 1's facade so
    the consent audit is written — with ``opt_in=False``, for the reason the
    module docstring gives at length.
    """
    from apps.messaging.models import ContactChannelIdentity

    # Scoped to the connection this node is about to send on — plus a pending
    # row, which the facade upgrades at first send. An unscoped "any email
    # identity for this contact" check answered yes for an identity on a
    # *different* email connection, so the node proceeded and `send_outbound`
    # then failed the message with `no_identity`, reporting a missing
    # prerequisite down the `default` edge instead of `error`.
    existing = (
        ContactChannelIdentity.objects.for_workspace(ctx.workspace_id)
        .filter(contact=ctx.contact, platform=Platform.EMAIL.value)
        .filter(Q(channel_connection=connection) | Q(channel_connection__isnull=True))
        .order_by("created_at")
        .first()
    )
    if existing is not None:
        return existing

    address = normalize_email(str(getattr(ctx.contact, "email", "") or ""))
    if not address:
        return None
    if len(address) > MAX_ADDRESS_CHARS:
        # `identities.bounded_key` hashes an over-long value rather than cutting
        # it, so storing this would produce a `sha256:…` identity that is not an
        # address at all and fails every later send with an opaque `no_address`.
        # Refusing here says so once, on the handle a flow author can branch on.
        logger.info(
            "Execution %s: this contact's email address is longer than %s characters.",
            ctx.execution.pk,
            MAX_ADDRESS_CHARS,
        )
        return None
    try:
        # A savepoint, because the identity table's ``(connection, address)``
        # constraint is deployment-wide across contacts: a soft-deleted contact
        # still owns its identity row, so re-importing the same address onto a
        # new contact collides. That is a real state — `delete_contact` is a
        # tombstone, not a row deletion — and it must come back as "no identity"
        # rather than as an IntegrityError that poisons the runner's
        # transaction.
        with transaction.atomic():
            return messaging.upsert_contact_identity(
                ctx.contact,
                Platform.EMAIL.value,
                address,
                source=CAPTURE_SOURCE,
                opt_in=False,
            )
    except IntegrityError:
        logger.info(
            "Execution %s: this address already belongs to another contact on that connection.",
            ctx.execution.pk,
        )
        return None
