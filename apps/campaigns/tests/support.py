"""Builders the campaign tests share.

A sequence is only interesting once it has a step, and a step needs a published
flow — three calls and a validation gate before anything under test happens. So
the fixtures here produce the shapes the tests actually reason about: a sequence
with N rungs, and a contact standing on one.
"""

from typing import Any

from apps.campaigns.models import DelayUnit, Sequence, SequenceStatus, SequenceStep
from apps.contacts.models import Contact
from apps.flows.services import create_flow, publish, save_draft
from apps.flows.tests.support import graph, node

__all__ = ["contact_for", "runnable_flow", "sequence_with", "step_for"]


def contact_for(workspace: Any, **fields: Any) -> Contact:
    """A saved contact in ``workspace``, named unless the test says otherwise."""
    fields.setdefault("first_name", "Ada")
    fields.setdefault("last_name", "Lovelace")
    return Contact.objects.create(workspace=workspace, **fields)


def runnable_flow(workspace: Any, name: str = "Step flow", *, tag: str = "") -> Any:
    """A published flow a sequence step can actually start.

    One ``action`` node that tags the contact, rather than a ``note``: a note is
    builder-only and does not count as an entry node, so a note-only graph is
    unpublishable. Tagging is the cheapest observable side effect there is — a
    test asserts a step ran by asking whether its tag is on the contact, without
    faking the messaging facade.
    """
    flow = create_flow(workspace=workspace, name=name)
    save_draft(flow, graph([node("a", "action", {"actions": [{"verb": "add_tag", "tag": tag or name}]})]))
    publish(flow)
    return flow


def step_for(
    sequence: Sequence,
    *,
    position: int,
    delay_value: int = 1,
    delay_unit: str = DelayUnit.DAYS,
    send_window: dict[str, Any] | None = None,
    flow: Any = None,
) -> SequenceStep:
    return SequenceStep.objects.create(
        workspace_id=sequence.workspace_id,
        sequence=sequence,
        position=position,
        flow=flow or runnable_flow(sequence.workspace, name=f"{sequence.name} step {position}"),
        delay_value=delay_value,
        delay_unit=delay_unit,
        send_window=send_window or {},
    )


def sequence_with(workspace: Any, *, steps: int = 1, name: str = "Onboarding", **step_kwargs: Any) -> Sequence:
    """An active sequence with ``steps`` rungs, each starting its own flow."""
    sequence = Sequence.objects.create(workspace=workspace, name=name, status=SequenceStatus.ACTIVE)
    for position in range(1, steps + 1):
        step_for(sequence, position=position, **step_kwargs)
    return sequence
