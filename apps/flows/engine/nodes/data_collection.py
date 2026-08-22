"""SPEC §11.8 — ask a question, validate the reply, record consent.

    Config: question text, reply_type (text, email, phone, number, date, url),
    target (custom_field or system email/phone), retry {max 3, invalid_text},
    timeout {delay, handle}. On valid input: save; if reply_type email/phone also
    update contact.email/phone and create/refresh the corresponding email/SMS
    identity with opt_in true recorded with timestamp + source (consent audit).
    Handles: default, timeout.

The node is only the *asking* half. Validation, storage and the consent call all
happen when the answer arrives, which is a different transaction hours later, so
they live in :mod:`apps.flows.engine.waits` next to the rest of the resume path
rather than here.

The consent half is the part worth reading twice. An email captured in a flow is
the deployment's legal basis for emailing that address, so it does not just land
in ``contact.email`` — it goes through contract 1's ``upsert_contact_identity``
with ``source="data_collection"`` and ``opt_in=True``, which is what stamps
``opt_in_at`` and ``opt_in_source`` on the identity row (SPEC §5). That audit
trail is the whole reason the facade owns identity writes.
"""

import logging

from apps.flows.engine.context import NodeContext
from apps.flows.engine.nodes.base import Node
from apps.flows.engine.registry import register_node
from apps.flows.engine.results import Continue, Fail, StepResult, Wait
from apps.flows.engine.sending import deliver, text_message
from apps.flows.engine.waits import REPLY_TYPES, data_collection_wait
from apps.flows.messaging import FacadeUnavailableError

__all__ = ["DataCollectionNode"]

logger = logging.getLogger(__name__)


@register_node
class DataCollectionNode(Node):
    """Ask, then wait for an answer that validates."""

    type = "data_collection"
    # Not in SPEC §7.1's inline-safe list. It sends and then parks, so the
    # webhook's first-reply budget buys nothing that enqueueing does not.
    synchronous_safe = False

    def execute(self, ctx: NodeContext) -> StepResult:
        reply_type = str(ctx.config.get("reply_type") or "")
        if reply_type not in REPLY_TYPES:
            return Fail(f"data_collection node {ctx.node_id}: {reply_type!r} is not a reply type")

        target = ctx.config.get("target")
        if not isinstance(target, dict):
            return Fail(f"data_collection node {ctx.node_id} has no target to save into")

        question = ctx.render(ctx.config.get("question"))
        if not question:
            return Fail(f"data_collection node {ctx.node_id} has no question to ask")

        try:
            outcome = deliver(ctx.execution, text_message(question), node_id=ctx.node_id)
        except FacadeUnavailableError as exc:
            return Fail(f"data_collection node {ctx.node_id}: {exc}")

        if not outcome.sent:
            # SPEC §9.5, same as any other send: the message failed, the flow
            # does not. Waiting for an answer to a question nobody received
            # would park the contact until the 30-day sweep.
            logger.info("Execution %s: question at node %s was not delivered.", ctx.execution.pk, ctx.node_id)
            return Continue("default")

        return Wait(
            data_collection_wait(
                ctx.node_id,
                reply_type=reply_type,
                target=target,
                retry=ctx.config.get("retry"),
                timeout=ctx.config.get("timeout"),
            )
        )
