"""Which platforms a flow will actually run on — the §16 capability-warning input.

Before this issue, the builder's capability warnings were computed from *every*
platform the workspace has connected. That is the right answer for a flow with
no triggers, and the wrong one as soon as a flow has some: a flow that only ever
runs from an SMS keyword should warn about its buttons, and a flow that only runs
on Telegram should not warn about anything SMS cannot do.

So the platform set comes from the triggers, and falls back to the workspace when
there are none — which keeps today's behaviour exactly until somebody adds a
trigger.
"""

from typing import Any

from apps.flows.capabilities import connected_platforms
from apps.flows.models import Trigger
from apps.flows.triggers.registry import spec_for

__all__ = ["declared_platforms_for_flow", "platforms_for_flow", "platforms_for_trigger"]


def platforms_for_trigger(trigger: Trigger, *, connected: set[str]) -> set[str]:
    """What one trigger contributes.

    A **bound** trigger contributes its connection's platform whatever the
    connection's status: the binding is the author's stated intent, and
    ``needs_reauth`` is a temporary condition rather than a change of plan —
    dropping the warning while a token is being refreshed would be worse than
    useless.

    An **unbound** one contributes its type's platforms narrowed to what the
    workspace has actually connected, because "all connections of a matching
    platform" is empty for a platform with no connections.
    """
    spec = spec_for(trigger.type)
    if spec is None:
        return set()
    connection = trigger.channel_connection if trigger.channel_connection_id is not None else None
    if connection is not None:
        return {str(connection.platform)}
    return set(spec.platforms) & connected


def declared_platforms_for_flow(flow: Any) -> tuple[str, ...]:
    """The platforms this flow's own triggers name. No fallback.

    :func:`platforms_for_flow` answers for **capability validation**, where a
    flow that names nothing still has to be warned about every channel it could
    run on — so it falls back to the workspace's connected platforms. A caller
    choosing a channel to *act* on needs the opposite guarantee. Under that
    fallback, a flow whose only trigger is an unbound Instagram one, in a
    workspace with only Telegram connected, answers ``("telegram",)``: the
    Instagram trigger narrows to nothing against the connected set, the empty
    result falls back, and the caller cannot tell that apart from a flow that
    genuinely runs anywhere. The builder's Test button then offered a Telegram
    chat for an Instagram flow instead of saying "connect Instagram".

    An empty result here means **this flow names no channel** — a
    channel-independent trigger (``api``, ``rule``, which resolve a channel from
    the contact at run time) or no enabled trigger at all. It never means "you
    have not connected the one it names", which is the distinction the fallback
    destroys.

    Not narrowed by what is connected, for the same reason: the whole point is
    to be able to say which channel is missing.
    """
    named: set[str] = set()
    triggers = (
        Trigger.objects.for_workspace(flow.workspace_id)
        .filter(flow=flow, enabled=True)
        .select_related("channel_connection")
    )
    for trigger in triggers:
        spec = spec_for(trigger.type)
        if spec is None:
            continue
        if not spec.platforms:
            # Channel-independent, so the flow as a whole runs anywhere and no
            # channel-specific answer is honest. Same short-circuit as
            # platforms_for_flow, for the same reason.
            return ()
        connection = trigger.channel_connection if trigger.channel_connection_id is not None else None
        named |= {str(connection.platform)} if connection is not None else set(spec.platforms)
    return tuple(sorted(named))


def platforms_for_flow(flow: Any) -> tuple[str, ...]:
    """Which platforms this flow can run on, for capability validation.

    Falls back to the workspace's connected platforms in two cases, and the
    second is easy to miss. The obvious one is a flow with no enabled trigger at
    all: nothing says where it runs, so every warning is still true.

    The other is a flow carrying a **channel-independent** trigger — ``api`` or
    ``rule``. Those name no platform, so they contribute nothing to the set; but
    ``fire_api_trigger`` resolves a channel from the contact's own identities and
    L6-A's rule triggers do the same, so such a flow really can run on any
    connected channel. Letting a Telegram trigger sitting beside an API trigger
    narrow the set would suppress the SMS warnings for a run that can genuinely
    happen on SMS.
    """
    connected = set(connected_platforms(flow.workspace_id))
    bound: set[str] = set()
    triggers = (
        Trigger.objects.for_workspace(flow.workspace_id)
        .filter(flow=flow, enabled=True)
        .select_related("channel_connection")
    )
    for trigger in triggers:
        spec = spec_for(trigger.type)
        if spec is not None and not spec.platforms:
            return tuple(sorted(connected))
        bound |= platforms_for_trigger(trigger, connected=connected)
    return tuple(sorted(bound or connected))
