"""Whether a flow can actually run, as warnings a reader can act on.

Validation answers "is this graph well formed?". It has never answered "will
anything ever reach it?", and those are different questions with the same
apparent answer: a flow imported from a template publishes clean, shows "Live"
and zero problems, and is inert — because its trigger arrived switched off, or
because it fires on a platform this workspace has not connected.

That combination is how somebody concludes the product is broken. Nothing in
the UI was wrong, exactly; it was silent about the only two facts that mattered.

These are **warnings**, never errors. SPEC §9.1 makes channel findings
non-blocking, and both states are normal on the way somewhere: you publish, then
connect the channel; you publish, then switch the trigger on. Blocking either
would stop a legitimate order of work. Saying nothing is what is not allowed.
"""

from typing import Any

from apps.common.platforms import Platform
from apps.flows.models import Trigger
from apps.flows.schema.issues import Issue
from apps.flows.triggers.registry import spec_for

__all__ = ["READINESS_CODES", "readiness_warnings"]

#: The codes this module emits. Named so a caller can drop them without matching
#: on message text, and so a test can assert the set has not quietly grown.
READINESS_CODES = ("flow_has_no_trigger", "flow_triggers_all_disabled", "flow_platform_not_connected")


def _label(platform: str) -> str:
    try:
        return str(Platform(platform).label)
    except ValueError:
        return platform


def readiness_warnings(flow: Any, *, connected: set[str]) -> list[Issue]:
    """Why this flow would not run today, or an empty list.

    ``connected`` is passed in rather than looked up so the caller that already
    computed it for the capability warnings does not pay for a second query.
    """
    triggers = list(
        Trigger.objects.for_workspace(flow.workspace_id).filter(flow=flow).select_related("channel_connection")
    )
    if not triggers:
        return [
            Issue(
                code="flow_has_no_trigger",
                message="Nothing starts this flow yet, so it will not run. Add a trigger under “When it runs”.",
                stage="graph",
            )
        ]

    enabled = [trigger for trigger in triggers if trigger.enabled]
    if not enabled:
        return [
            Issue(
                code="flow_triggers_all_disabled",
                message=(
                    "Every trigger on this flow is switched off, so it will not run. "
                    "Switch one on under “When it runs”."
                ),
                stage="graph",
            )
        ]

    # A trigger no connected channel can deliver. Bound triggers are excluded:
    # the connection exists by definition, and whether its token needs
    # refreshing is the channels page's business, not this one's.
    missing: dict[str, None] = {}
    for trigger in enabled:
        if trigger.channel_connection_id is not None:
            continue
        spec = spec_for(trigger.type)
        # No platforms at all means channel-independent (``api``, ``rule``),
        # which resolves a channel from the contact rather than from the trigger.
        if spec is None or not spec.platforms:
            continue
        if not (set(spec.platforms) & connected):
            for platform in sorted(spec.platforms):
                missing[platform] = None

    if not missing:
        return []

    names = [_label(platform) for platform in missing]
    joined = names[0] if len(names) == 1 else ", ".join(names[:-1]) + f" or {names[-1]}"
    # Naming the platforms once. Saying them at both ends read "starts from
    # Instagram or Facebook Messenger, and no Instagram or Facebook Messenger
    # account is connected" — true, and nobody finishes reading it.
    tail = "and it is not connected" if len(names) == 1 else "and none of those is connected"
    return [
        Issue(
            code="flow_platform_not_connected",
            message=(
                f"Nothing can reach this flow: it starts on {joined}, {tail}. "
                f"Connect an account under Settings, then Channels."
            ),
            stage="graph",
        )
    ]
