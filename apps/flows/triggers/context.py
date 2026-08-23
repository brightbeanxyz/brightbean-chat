"""What every routing hook sees, and how it is assembled.

One object built once per event, so five stages cannot disagree about whether a
conversation was paused, and so a later stream's hook reads the same contact the
built-in ones did rather than looking it up again.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from django.utils import timezone

from apps.channels.events import NormalizedEvent
from apps.flows import messaging as messaging_facade
from apps.flows.compat import installed_model
from apps.flows.triggers.budget import InlineBudget
from apps.flows.triggers.hooks import Stage

__all__ = ["RoutingContext", "RoutingMode", "build_context"]

logger = logging.getLogger(__name__)


class RoutingMode(StrEnum):
    """Where this run is happening, which is what the lock and budget differ on."""

    #: In the webhook request. Non-blocking lock, SPEC §7.1's 1.5 s budget.
    INLINE = "inline"
    #: In the worker. Blocking lock, no budget — there is no client waiting.
    WORKER = "worker"


@dataclass
class RoutingContext:
    """One event's view of the world, for the whole stage chain."""

    event: NormalizedEvent
    connection: Any
    budget: InlineBudget
    mode: RoutingMode
    identity: Any | None = None
    contact: Any | None = None
    conversation: Any | None = None

    #: Read from ``conversation.automation_paused_until``, never written.
    #:
    #: The short name is not a style choice. ``apps/messaging/tests/test_write_sites.py``
    #: walks the AST of every module under ``apps/`` and fails the build on an
    #: assignment whose attribute is ``automation_paused_until`` — contract 3
    #: gives that column exactly one write site. Naming the field ``paused_until``
    #: keeps this a read and keeps the guard honest, rather than teaching the
    #: guard about an exception.
    paused_until: datetime | None = None

    #: Set by the runner before each stage, so a hook can log where it is.
    stage: Stage = Stage.HARD_OPTOUT
    #: Free space for hooks to leave findings for later stages.
    notes: dict[str, Any] = field(default_factory=dict)

    @property
    def workspace(self) -> Any:
        """The tenant this event belongs to.

        A property rather than a field: the only reader is the hand-off, which
        needs a ``Workspace`` instance because ``queueing.schedule`` refuses an
        id. The connection arrives from ``resolve_connection`` without
        ``select_related("workspace")``, so materialising it eagerly would spend
        a query per event on a value most events never look at.
        """
        return self.connection.workspace

    @property
    def is_paused(self) -> bool:
        """Whether automation is suppressed (SPEC §14's agent takeover).

        Computed from a value fixed when the context was built, so every stage
        gets the same answer even if the pause lapses mid-chain.
        """
        return self.paused_until is not None and self.paused_until > timezone.now()

    @property
    def can_run_engine(self) -> bool:
        """Whether there is a contact to run a flow for.

        False for a comment: ``apps/messaging/ingest.py`` deliberately creates no
        contact for one, so the comment path matches a trigger and hands off to
        the platform-specific private reply rather than starting a flow here.
        """
        return self.contact is not None


def build_context(
    connection: Any,
    event: NormalizedEvent,
    budget: InlineBudget,
    *,
    mode: RoutingMode,
    now: datetime | None = None,
) -> RoutingContext | None:
    """Assemble the context, or ``None`` when this event is not ours to route.

    Two reads, both scoped, both read-only. It resolves **nothing**: identity
    creation belongs to ``apps.messaging.ingest``, which ran first and decided
    per event type whether a contact should exist. A routing stage that resolved
    on its own would create one for a comment, which is exactly the
    "one viral post becomes a contact-spam amplifier" outcome that module's
    docstring says it is avoiding.
    """
    if not _belongs_to(connection, event):
        return None

    context = RoutingContext(event=event, connection=connection, budget=budget, mode=mode)

    identity = _identity_for(connection, event)
    if identity is not None:
        context.identity = identity
        context.contact = identity.contact
        conversation = _conversation_for(connection, identity.contact)
        if conversation is not None:
            context.conversation = conversation
            context.paused_until = conversation.automation_paused_until
    return context


def _identity_for(connection: Any, event: NormalizedEvent) -> Any | None:
    """The identity persistence wrote for this event, if it wrote one."""
    model = installed_model("messaging", "apps.messaging", "ContactChannelIdentity")
    if model is None:  # pragma: no cover - messaging is installed in every deployment
        return None
    address = messaging_facade.bounded_address(event.platform_user_id)
    if not address:
        return None
    return (
        model.objects.for_workspace(connection.workspace_id)
        .filter(channel_connection=connection, platform_user_id=address)
        .select_related("contact")
        .first()
    )


def _conversation_for(connection: Any, contact: Any) -> Any | None:
    model = installed_model("messaging", "apps.messaging", "Conversation")
    if model is None:  # pragma: no cover - see above
        return None
    return (
        model.objects.for_workspace(connection.workspace_id)
        .filter(contact=contact, channel_connection=connection)
        .first()
    )


def _belongs_to(connection: Any, event: NormalizedEvent) -> bool:
    """A tenancy backstop, three lines rather than an import of a private helper.

    The webhook endpoint has already checked this, and persistence checks it
    again. Routing is the stage that starts flows and sends messages, so it
    checks too: the cost is an attribute comparison and the failure it prevents
    is one workspace's event running another's automation.
    """
    owner = getattr(event, "connection", None)
    if owner is None:
        return True
    return owner.pk == connection.pk and owner.workspace_id == connection.workspace_id
