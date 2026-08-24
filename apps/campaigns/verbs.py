"""The two action-node verbs SPEC §11.2 reserves for sequences (contract 5).

The *schemas* already ship in ``apps/flows/schema/nodes.py``, which is why the
flow builder has been able to offer these verbs since L2-D. What was missing was
the runtime: ``apps/flows/engine/nodes/action.py`` says an unregistered verb
"logs a warning and moves on", and that documented no-op is what this module
replaces. There is deliberately no second schema here.

**The config holds an id, not a name.** ``tag`` and ``field`` are 200-character
*names* in that schema; ``member``, ``sequence`` and ``flow_id`` are 64-character
*ids*. That is the convention ``apps/flows/picklists.py`` feeds the builder, and
it is why this module resolves through ``Sequence.objects.for_workspace(...)``
rather than by name: a graph is editable by anyone with ``edit_flows``, so a
hand-edited id must not be able to name another tenant's sequence
(SECURITY-BASELINE §1) — exactly the check ``_assign_conversation`` makes for a
member id.

**Refusals raise.** The action node catches ``ValueError`` per verb, logs it and
runs the rest of the list, which is SPEC §11.2's "always Continue". So a stale
sequence id costs the flow a log line and not the tag it was also supposed to
add.
"""

import logging
from typing import Any
from uuid import UUID

from apps.campaigns import services
from apps.campaigns.errors import CampaignsError
from apps.campaigns.models import Sequence
from apps.flows.engine.registry import register_verb

__all__ = ["subscribe_sequence", "unsubscribe_sequence"]

logger = logging.getLogger(__name__)


def subscribe_sequence(ctx: Any, step: dict[str, Any]) -> None:
    """SPEC §11.2's ``subscribe_sequence(sequence)``.

    Re-subscribing somebody already walking the sequence restarts them at step 1,
    which is SPEC §12's documented behaviour rather than this verb's own idea —
    see :func:`apps.campaigns.services.subscribe`.
    """
    sequence = _sequence(ctx, step)
    services.subscribe(sequence, ctx.contact, source="flow")


def unsubscribe_sequence(ctx: Any, step: dict[str, Any]) -> None:
    """SPEC §11.2's ``unsubscribe_sequence``.

    Silent for a contact who is not enrolled: the verb's promise is that they are
    not on the sequence afterwards, and announcing an unsubscribe that did not
    happen would fire every ``sequence_unsubscribed`` rule trigger in the
    workspace on every re-run of the flow.
    """
    sequence = _sequence(ctx, step)
    services.unsubscribe(sequence, ctx.contact)


def _sequence(ctx: Any, step: dict[str, Any]) -> Sequence:
    """Resolve the config's ``sequence`` id inside this execution's workspace."""
    raw = step.get("sequence")
    try:
        pk = UUID(str(raw))
    except (ValueError, AttributeError, TypeError) as exc:
        raise CampaignsError("That is not a sequence id.") from exc
    sequence = Sequence.objects.for_workspace(ctx.workspace_id).filter(pk=pk).first()
    if sequence is None:
        raise CampaignsError("No such sequence in this workspace.")
    return sequence


register_verb("subscribe_sequence", subscribe_sequence)
register_verb("unsubscribe_sequence", unsubscribe_sequence)
