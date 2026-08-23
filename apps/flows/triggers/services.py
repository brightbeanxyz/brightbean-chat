"""Creating, editing and reordering triggers, plus the summaries the API serves.

Every write goes through here rather than through the views, for the reason
``apps.flows.services`` already exists: the reorder is a multi-row operation
under a lock, and the validation gate is the same whether the caller is the HTMX
panel or (later) the public API.
"""

import logging
from typing import Any

from django.db import transaction

from apps.flows.models import Flow, Trigger, TriggerType
from apps.flows.schema.issues import Issue
from apps.flows.triggers.registry import spec_for
from apps.flows.triggers.validation import validate_config

__all__ = [
    "PRIORITY_STEP",
    "TriggerValidationError",
    "api_trigger",
    "create_trigger",
    "delete_trigger",
    "duplicate_refs",
    "move_trigger",
    "set_enabled",
    "summaries",
    "triggers_for",
    "update_trigger",
    "workspace_triggers",
]

logger = logging.getLogger(__name__)

#: Renormalised priorities are spaced, so "move this one up" never has to
#: renumber the whole list and two triggers never end up tied by accident.
PRIORITY_STEP = 10


class TriggerValidationError(ValueError):
    """A trigger was refused. ``issues`` is what to render."""

    def __init__(self, issues: list[Issue]) -> None:
        super().__init__("This trigger's configuration is not valid.")
        self.issues = issues


def triggers_for(flow: Flow) -> Any:
    """This flow's triggers in match order."""
    return (
        Trigger.objects.for_workspace(flow.workspace_id)
        .filter(flow=flow)
        .select_related("channel_connection")
        .order_by("priority", "created_at", "id")
    )


def workspace_triggers(workspace_id: Any, *, lock: bool = False) -> Any:
    """**Every** trigger in the workspace, in match order.

    Priority is workspace-wide, not per flow — SPEC §10 orders the whole
    candidate set and takes the first match, and
    :func:`apps.flows.triggers.matching.candidates` queries across the workspace
    to honour that. So allocation and reordering have to work in the same scope:
    numbering each flow's triggers from zero gives every flow a priority-0
    trigger, and two flows with an overlapping keyword are then separated only by
    the hidden ``created_at`` tie-break, which no user can change.

    ``lock`` takes ``select_for_update`` over the rows being renumbered, which is
    the serialisation point a reorder needs — the flow row is not, because a
    reorder now touches rows belonging to other flows.
    """
    queryset = Trigger.objects.for_workspace(workspace_id).order_by("priority", "created_at", "id")
    return queryset.select_for_update() if lock else queryset


def create_trigger(
    flow: Flow,
    *,
    trigger_type: str,
    config: dict[str, Any] | None = None,
    connection: Any = None,
    enabled: bool = True,
) -> Trigger:
    """Add a trigger to the workspace, last in match order.

    "Last" is workspace-wide: see :func:`workspace_triggers` for why per-flow
    numbering would make overlapping triggers unorderable.
    """
    spec = spec_for(trigger_type)
    if spec is None:
        raise TriggerValidationError([_issue(f"{trigger_type!r} is not a trigger type.", "type")])
    payload = config if config is not None else spec.default_config()
    _check(trigger_type, payload, connection)

    with transaction.atomic():
        locked = Flow.objects.for_workspace(flow.workspace_id).select_for_update().get(pk=flow.pk)
        last = (
            workspace_triggers(flow.workspace_id, lock=True)
            .order_by("-priority")
            .values_list("priority", flat=True)
            .first()
        )
        trigger = Trigger(
            flow=locked,
            channel_connection=connection,
            type=trigger_type,
            config_json=payload,
            enabled=enabled,
            priority=(last + PRIORITY_STEP) if last is not None else 0,
        )
        trigger.save()
    return trigger


def update_trigger(
    trigger: Trigger,
    *,
    config: dict[str, Any] | None = None,
    connection: Any = None,
    connection_given: bool = False,
) -> Trigger:
    """Change a trigger's configuration and, optionally, its binding.

    ``connection_given`` rather than a sentinel default: ``None`` is a *real*
    value here — it is how a trigger says "every connection of a matching
    platform" — so "no connection" and "leave the connection alone" cannot share
    one argument without one of them silently winning.
    """
    payload = trigger.config_json if config is None else config
    target = connection if connection_given else trigger.channel_connection
    _check(trigger.type, payload, target)

    trigger.config_json = payload
    fields = ["config_json", "updated_at"]
    if connection_given:
        trigger.channel_connection = target
        fields.append("channel_connection")
    trigger.save(update_fields=fields)
    return trigger


def set_enabled(trigger: Trigger, enabled: bool) -> Trigger:
    """Turn a trigger on or off. Disabled triggers are never candidates."""
    trigger.enabled = enabled
    trigger.save(update_fields=["enabled", "updated_at"])
    return trigger


def delete_trigger(trigger: Trigger) -> None:
    trigger.delete()


def move_trigger(trigger: Trigger, *, direction: str) -> Trigger:
    """Swap a trigger with its neighbour in match order.

    The order is the **workspace's**, not the flow's, because that is the order
    the matcher walks: a keyword trigger on one flow and a keyword trigger on
    another compete for the same message, and "run this one first" has to be able
    to settle that. The neighbour a move swaps with may therefore belong to a
    different flow — which is why the panel says priority is workspace-wide and
    shows the number on every row.

    Renormalising to ``0, 10, 20, …`` first does two jobs: it breaks any existing
    ties, so "up" always has a well-defined meaning, and together with
    ``select_for_update`` over the same rows it keeps two concurrent reorders
    from interleaving into an order neither asked for.
    """
    if direction not in {"up", "down"}:
        raise TriggerValidationError([_issue(f"{direction!r} is not a direction.", "direction")])

    with transaction.atomic():
        ordered = list(workspace_triggers(trigger.workspace_id, lock=True))
        for position, row in enumerate(ordered):
            wanted = position * PRIORITY_STEP
            if row.priority != wanted:
                row.priority = wanted
                row.save(update_fields=["priority", "updated_at"])

        index = next((position for position, row in enumerate(ordered) if row.pk == trigger.pk), None)
        if index is None:  # pragma: no cover - the caller just fetched it
            return trigger
        neighbour = index - 1 if direction == "up" else index + 1
        if not 0 <= neighbour < len(ordered):
            return ordered[index]

        here, there = ordered[index], ordered[neighbour]
        here.priority, there.priority = there.priority, here.priority
        here.save(update_fields=["priority", "updated_at"])
        there.save(update_fields=["priority", "updated_at"])
        return here


def duplicate_refs(flow: Flow, ref: str, *, exclude: Any = None) -> bool:
    """Whether another ref-URL trigger in this workspace already uses ``ref``.

    Checked here rather than by a unique index. An index over a jsonb key means
    a functional expression index that knows nothing about trimming, and the
    check has to happen against the *workspace* — two flows sharing a ref is the
    ambiguity, not two triggers on one flow.
    """
    queryset = Trigger.objects.for_workspace(flow.workspace_id).filter(type=TriggerType.REF_URL, config_json__ref=ref)
    if exclude is not None:
        queryset = queryset.exclude(pk=exclude.pk)
    return queryset.exists()


def api_trigger(flow: Flow, *, key: str = "") -> Trigger | None:
    """The ``api`` trigger #25's flow-start endpoint should fire.

    SPEC §10 says the type is "fired via public API flow-start endpoint", so it
    has no matcher and never appears in a candidate query. ``key`` selects
    between several on one flow; the lowest priority wins when it is blank,
    which is the same first-match-wins rule everything else uses.
    """
    for trigger in triggers_for(flow).filter(type=TriggerType.API, enabled=True):
        if not key or (trigger.config_json or {}).get("key") == key:
            return trigger
    return None


def summaries(flow: Flow) -> list[dict[str, Any]]:
    """The read-only trigger list the builder API serves.

    ``config_json`` is deliberately absent. The builder does not edit triggers,
    and shipping the raw config would make the React store a second place a
    trigger's configuration lives — with the panel as the first.
    """
    from apps.flows.triggers.platforms import platforms_for_trigger

    connected = set(_connected(flow))
    rows: list[dict[str, Any]] = []
    for trigger in triggers_for(flow):
        connection = trigger.channel_connection
        rows.append(
            {
                "id": str(trigger.pk),
                "type": trigger.type,
                "type_label": trigger.get_type_display(),
                "enabled": trigger.enabled,
                "priority": trigger.priority,
                "summary": describe(trigger),
                "connection": (
                    None
                    if connection is None
                    else {
                        "id": str(connection.pk),
                        "label": connection.display_name,
                        "platform": connection.platform,
                    }
                ),
                "platforms": sorted(platforms_for_trigger(trigger, connected=connected)),
            }
        )
    return rows


def describe(trigger: Trigger) -> str:
    """One line of human-readable configuration, for the panel and the API."""
    config = trigger.config_json or {}
    if trigger.type == TriggerType.KEYWORD:
        words = [str(item.get("text", "")) for item in config.get("keywords") or () if isinstance(item, dict)]
        if not words:
            return "No keywords yet"
        shown = ", ".join(words[:3])
        return shown if len(words) <= 3 else f"{shown} and {len(words) - 3} more"
    if trigger.type == TriggerType.REF_URL:
        return f"Reference “{config.get('ref') or '—'}”"
    if trigger.type == TriggerType.COMMENT:
        scope = "specific posts" if config.get("post_scope") == "specific" else "any post"
        return f"Comments on {scope}"
    spec = spec_for(trigger.type)
    return spec.description if spec is not None else ""


def _connected(flow: Flow) -> tuple[str, ...]:
    from apps.flows.capabilities import connected_platforms

    return connected_platforms(flow.workspace_id)


def _check(trigger_type: str, config: Any, connection: Any) -> None:
    issues = validate_config(trigger_type, config)
    spec = spec_for(trigger_type)
    if connection is not None and spec is not None:
        if not spec.bindable:
            issues = [*issues, _issue(f"A {spec.label.lower()} trigger is not tied to a channel.", "connection")]
        elif connection.platform not in spec.platforms:
            issues = [
                *issues,
                _issue(f"{spec.label} triggers do not run on {connection.get_platform_display()}.", "connection"),
            ]
    if issues:
        raise TriggerValidationError(list(issues))


def _issue(message: str, path: str) -> Issue:
    return Issue(code="invalid_config_value", message=message, stage="document", node_id=None, path=path)
