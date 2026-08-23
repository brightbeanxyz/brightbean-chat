"""The §11.4 condition builder's data payload, for every page that renders it.

Two pages do now. The contact list has always had the builder; issue #24's
inbox-rule editor embeds the same ``templates/contacts/_filter_bar.html`` for the
contact half of a rule's condition, because a rule and a segment ask the same
question of a contact and ROADMAP contract 8 gives that question one language.

Its own module rather than a function on the contact list, so the second consumer
imports a payload builder instead of importing another app's view module.
"""

from typing import Any

from apps.common.platforms import Platform
from apps.contacts import conditions
from apps.contacts.conditions import CONDITION_SCHEMA
from apps.contacts.models import CustomField, Segment, Tag

__all__ = ["builder_config"]


def builder_config(workspace: Any, *, document: dict[str, Any] | None = None, segment_id: str = "") -> dict[str, Any]:
    """Everything the §11.4 filter builder needs to render, in one payload.

    Public and parameter-shaped rather than private and ``ContactQuery``-shaped,
    because the contact list is no longer the only page with this builder on it:
    issue #24's inbox-rule editor embeds ``templates/contacts/_filter_bar.html``
    for the contact half of a rule's condition. Two copies of this payload would
    drift the day somebody adds an operator to :mod:`apps.contacts.conditions`,
    which is precisely what its ``x-brightbean`` extension block exists to
    prevent.

    ``CONDITION_SCHEMA["x-brightbean"]`` already carries the operator tables, the
    valueless-operator set, the operator labels, the system fields, the relative
    units, which sources this deployment cannot evaluate, and the limits — that
    extension block exists precisely so a consumer does not have to keep a second
    copy. So the builder reads it, and an operator added to
    :mod:`apps.contacts.conditions` shows up in this UI with no edit here.

    Only two things are added, because neither can live in a static schema: each
    source's label, evaluability and owning issue from the registry, and this
    workspace's own tags, fields and segments.

    One dict rather than six template variables, because it is one ``x-data``
    argument — and assembling it in the template would put the payload's shape
    somewhere Python cannot see it.
    """
    registry = conditions.sources()
    return {
        "sources": [
            {
                "name": name,
                "label": registry[name].label,
                "keyKind": registry[name].key_kind,
                "evaluable": registry[name].is_evaluable,
                # Carried so a greyed-out row can say *why* it is unavailable —
                # "arrives with issue #22" beats a control that does nothing.
                "owner": registry[name].owner,
            }
            for name in conditions.SOURCE_NAMES
        ],
        "vocabulary": CONDITION_SCHEMA["x-brightbean"],
        "platforms": [{"value": value, "label": label} for value, label in Platform.choices],
        "tags": [
            {"value": str(row.pk), "label": row.name} for row in Tag.objects.for_workspace(workspace).order_by("name")
        ],
        "fields": [
            {"value": str(row.pk), "label": row.name, "type": row.type}
            for row in CustomField.objects.for_workspace(workspace).order_by("name")
        ],
        "segments": [
            {"value": str(row.pk), "label": row.name}
            for row in Segment.objects.for_workspace(workspace).order_by("name")
        ],
        # The document the builder hydrates from. The caller passes what it
        # holds rather than this function re-reading the URL, so a segment
        # loaded off disk round-trips exactly as stored instead of through a
        # re-serialisation that could normalise it — which is the acceptance
        # criterion the contact list is judged on, and the property the rule
        # editor needs for exactly the same reason.
        "document": document or {},
        "segmentId": segment_id,
    }
