"""Which flow node a message came from (SPEC §9.4, §18).

There is no ``node_id`` column on ``message``. There does not need to be one:
SPEC §9.4 fixes the outbound idempotency key as
``exec:{execution_id}:node:{node_id}:{attempt}``, minted in exactly one place —
:func:`apps.flows.messaging.message_idempotency_key` — and stored under a unique
index by the facade. So the key *is* the attribution, and reading it back is the
counters' half of a contract the send path already keeps.

**Everything that is not a node send fails to parse, and that is the filter.**
An agent's inbox reply, an API send, a broadcast's own bookkeeping row and an
inbox note all carry keys of other shapes; none of them belongs in a per-node
counter, and none of them reaches one.

--------------------------------------------------------------------------
Preview runs stop here
--------------------------------------------------------------------------

``FlowExecution.preview`` marks a run started from the builder's "test on
Telegram" (SPEC §16, issue #12). Three modules promise it is kept out of these
numbers — ``apps/flows/models.py``, ``apps/flows/engine/runner.py`` and
``apps/broadcasts/handlers.py`` — and this function is where that promise is
kept, once, for every counter. A preview is a *real* execution with real sends,
so nothing upstream can be relied on to have filtered it out.

The lookup is workspace-scoped rather than ``unscoped()``: the message names its
own workspace, and an execution id that resolves to somebody else's row must
attribute nothing rather than write a counter into another tenant's flow.
"""

import re
from dataclasses import dataclass
from typing import Any

from django.core.exceptions import ValidationError

from apps.flows.schema.envelope import ID_PATTERN

__all__ = ["NodeRef", "node_for", "parse_idempotency_key"]

#: SPEC §9.4's key, read back. The node id's own character class comes from
#: ``apps.flows.schema.envelope.ID_PATTERN`` — letters, digits, ``_`` and ``-``,
#: so it can never contain the ``:`` that separates the parts — and is spliced in
#: rather than restated, so a widened id class cannot leave this regex behind.
_KEY_RE = re.compile(
    rf"^exec:(?P<execution>[0-9a-fA-F-]{{36}}):node:(?P<node>{ID_PATTERN.strip('^$')}):(?P<attempt>\d+)$"
)


@dataclass(frozen=True)
class NodeRef:
    """The flow and node one message is attributable to."""

    flow_id: Any
    node_id: str


def parse_idempotency_key(key: str) -> tuple[str, str] | None:
    """``(execution_id, node_id)`` for a node send, ``None`` for anything else."""
    match = _KEY_RE.match(key or "")
    if match is None:
        return None
    return match.group("execution"), match.group("node")


def node_for(message: Any) -> NodeRef | None:
    """The node ``message`` came from, or ``None`` if it is not counted.

    ``None`` for three separate reasons, all of them ordinary: the key names no
    node, the execution is gone, or the execution is a preview.
    """
    from apps.flows.models import FlowExecution

    parsed = parse_idempotency_key(str(getattr(message, "idempotency_key", "") or ""))
    if parsed is None:
        return None
    execution_id, node_id = parsed

    try:
        row = (
            FlowExecution.objects.for_workspace(message.workspace_id)
            .filter(pk=execution_id)
            .values("flow_id", "preview")
            .first()
        )
    except (ValidationError, ValueError, TypeError):
        # The regex above accepts every 36-character run of hex and dashes, which
        # is UUID-*shaped* without being a UUID, and a UUIDField raises rather
        # than not-matching for one. Only reachable from a hand-written key, so
        # this is "not a node send" rather than an error — the same three
        # exceptions, in the same order, as
        # ``apps.channels.views_unsubscribe._identity``.
        return None
    if row is None or row["preview"]:
        return None
    return NodeRef(flow_id=row["flow_id"], node_id=node_id)
