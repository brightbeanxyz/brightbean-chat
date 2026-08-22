"""SPEC §11.2 — the action node and the verbs it can run.

    Config: actions[] executed in order: add_tag(tag), remove_tag,
    set_field(field, value|placeholder), clear_field, subscribe_sequence(sequence),
    unsubscribe_sequence, open_conversation, close_conversation,
    assign_conversation(member), notify_members(member_ids, via, text).
    Handles: default. Always Continue.

"Always Continue" is the load-bearing half of that sentence. An action node is a
list of side effects, and one of them failing — a tag whose name no longer
exists, a member who has left the workspace — must not end the run. Each verb is
attempted, problems are logged, and the node continues. The failures that *are*
worth stopping for are the ones a node cannot even attempt, and those raise
rather than returning ``Fail``, so the queue's transaction rolls the whole step
back and retries it (see :mod:`apps.flows.engine.runner`).

**Two addressing conventions, both taken from the schema.** ``tag`` and ``field``
are 200-character strings and hold *names*; ``member``, ``sequence`` and
``flow_id`` are 64-character strings and hold *ids*. That is not a coincidence in
``apps/flows/schema/nodes.py`` — it matches what ``apps/flows/picklists.py``
hands the builder, and it matches SPEC §9.2's "custom fields by name" for the
renderer. A tag renamed in the CRM therefore breaks a flow that adds it, which is
the trade ManyChat makes too and the one the schema already chose.

**Field resolution and coercion are not here** either. ``set_field`` shares
:mod:`apps.flows.engine.fields` with the External Request node (L4-E), which
writes custom fields from JSON responses rather than from rendered text; the two
directions meet at ``coerce_value`` and the adaptation belongs in one place.

**Sequence verbs are not here.** ``subscribe_sequence`` and
``unsubscribe_sequence`` have schemas (so the builder can offer them today) and
no runtime until L6-A registers one. Reaching one logs a warning and moves on,
which is what "unknown verb in a graph → validation error at publish, warning log
at runtime" means: the *schema* registry is what rejects a verb nobody has ever
declared, at publish time; this is the other case, a declared verb whose owner
has not landed.
"""

import logging
from typing import Any
from uuid import UUID

from apps.contacts.errors import ContactsError
from apps.contacts.models import Tag
from apps.contacts.services import (
    add_tag,
    clear_field_value,
    get_or_create_tag,
    remove_tag,
    set_field_value,
)
from apps.flows import messaging
from apps.flows.engine.context import NodeContext
from apps.flows.engine.fields import custom_field_by_name, typed_for
from apps.flows.engine.nodes.base import Node
from apps.flows.engine.registry import register_node, register_verb, verb_handler
from apps.flows.engine.results import Continue, StepResult
from apps.flows.notifications import event_for_via

__all__ = ["ActionNode"]

logger = logging.getLogger(__name__)


@register_node
class ActionNode(Node):
    """Runs its verbs in order and always continues."""

    type = "action"
    synchronous_safe = True

    def execute(self, ctx: NodeContext) -> StepResult:
        steps = ctx.config.get("actions") or []
        for step in steps:
            if not isinstance(step, dict):
                continue
            verb = step.get("verb")
            if not isinstance(verb, str):
                continue
            handler = verb_handler(verb)
            if handler is None:
                logger.warning(
                    "Execution %s: action node %s names verb %r, which has no runtime in this deployment; skipping it.",
                    ctx.execution.pk,
                    ctx.node_id,
                    verb,
                )
                continue
            try:
                handler(ctx, step)
            except (ContactsError, messaging.FacadeUnavailableError, LookupError, ValueError) as exc:
                # Diagnosable and specific to this verb: a mistyped value, a
                # deleted object, messaging not installed. The other verbs in
                # the list still deserve to run.
                logger.warning(
                    "Execution %s: action node %s verb %r failed: %s",
                    ctx.execution.pk,
                    ctx.node_id,
                    verb,
                    exc,
                )
        return Continue("default")


# ---------------------------------------------------------------------------
# Built-in verbs (SPEC §11.2)
# ---------------------------------------------------------------------------


def _add_tag(ctx: NodeContext, step: dict[str, Any]) -> None:
    """Tag the contact, creating the tag if the workspace has not got it.

    Create-on-use rather than refuse-if-missing: a flow is often the thing that
    introduces a tag, and an author who typed a new name in the builder means to
    start using it. ``get_or_create_tag`` is case-insensitive, so this cannot
    fork "Lead" and "lead" into two tags.
    """
    name = _text(step.get("tag"))
    if not name:
        raise ValueError("add_tag needs a tag name.")
    tag, created = get_or_create_tag(ctx.workspace, name)
    if created:
        logger.info("Execution %s created tag %r", ctx.execution.pk, tag.name)
    add_tag(ctx.contact, tag)


def _remove_tag(ctx: NodeContext, step: dict[str, Any]) -> None:
    """Untag the contact. A tag the workspace does not have is already removed."""
    name = _text(step.get("tag"))
    if not name:
        raise ValueError("remove_tag needs a tag name.")
    tag = Tag.objects.for_workspace(ctx.workspace_id).filter(name__iexact=name).first()
    if tag is None:
        logger.debug("Execution %s: no tag named %r to remove.", ctx.execution.pk, name)
        return
    remove_tag(ctx.contact, tag)


def _set_field(ctx: NodeContext, step: dict[str, Any]) -> None:
    """Write a custom field, rendering ``{{placeholders}}`` in the value first."""
    field = custom_field_by_name(ctx, step.get("field"))
    if field is None:
        return
    rendered = ctx.render(step.get("value"))
    set_field_value(ctx.contact, field, typed_for(field, rendered))


def _clear_field(ctx: NodeContext, step: dict[str, Any]) -> None:
    field = custom_field_by_name(ctx, step.get("field"))
    if field is None:
        return
    clear_field_value(ctx.contact, field)


def _open_conversation(ctx: NodeContext, _step: dict[str, Any]) -> None:
    messaging.open_conversation(ctx.contact, ctx.connection)


def _close_conversation(ctx: NodeContext, _step: dict[str, Any]) -> None:
    messaging.close_conversation(ctx.contact, ctx.connection)


def _assign_conversation(ctx: NodeContext, step: dict[str, Any]) -> None:
    """Hand the conversation to a member — of *this* workspace, checked.

    The member id comes out of a graph document, and a graph document is
    editable by anyone with ``edit_flows``. Resolving it through this
    workspace's memberships rather than through ``User.objects.get`` is what
    stops a hand-edited flow from assigning conversations to somebody in another
    tenant (SECURITY-BASELINE §1).
    """
    user = _member(ctx, step.get("member"))
    if user is None:
        return
    messaging.assign_conversation(ctx.contact, user, ctx.connection)


def _notify_members(ctx: NodeContext, step: dict[str, Any]) -> None:
    """In-app (and optionally emailed) alert to named members.

    ``via`` picks between two registered events rather than a flag on the send;
    see :mod:`apps.flows.notifications` for why that is the shape.
    """
    from apps.notifications.engine import notify

    raw_ids = step.get("member_ids")
    users = [user for user in (_member(ctx, value) for value in raw_ids or []) if user is not None]
    if not users:
        logger.warning("Execution %s: notify_members named nobody in this workspace.", ctx.execution.pk)
        return
    notify(
        ctx.workspace,
        event_for_via(str(step.get("via") or "in_app")),
        users=users,
        context={
            # "{actor_name} mentioned you" — the actor is the automation, which
            # is the honest answer and the one an admin can act on.
            "actor_name": ctx.execution.flow.name,
            "message": ctx.render(step.get("text")),
        },
    )


for _verb, _handler in (
    ("add_tag", _add_tag),
    ("remove_tag", _remove_tag),
    ("set_field", _set_field),
    ("clear_field", _clear_field),
    ("open_conversation", _open_conversation),
    ("close_conversation", _close_conversation),
    ("assign_conversation", _assign_conversation),
    ("notify_members", _notify_members),
):
    register_verb(_verb, _handler)


# ---------------------------------------------------------------------------
# Resolution helpers
# ---------------------------------------------------------------------------


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _member(ctx: NodeContext, value: Any) -> Any | None:
    """A workspace member's user, by user id, or ``None`` with a warning."""
    from apps.members.models import WorkspaceMembership

    try:
        user_id = UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        logger.warning("Execution %s: %r is not a member id.", ctx.execution.pk, value)
        return None
    membership = (
        WorkspaceMembership.objects.filter(workspace_id=ctx.workspace_id, user_id=user_id)
        .select_related("user")
        .first()
    )
    if membership is None:
        logger.warning(
            "Execution %s: member %s is not in this workspace; skipping.",
            ctx.execution.pk,
            user_id,
        )
        return None
    return membership.user
