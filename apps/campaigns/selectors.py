"""Read models for the sequence pages. Queries only — no writes, no events.

Split out of ``views.py`` for the reason ``apps/inbox/selectors.py`` gives: the
views are about permissions and HTMX, and the interesting part of this feature's
read path is which counts are one query and which would be N+1.

Two counts, and they answer different questions:

``subscriber_count``
    How many people are on the sequence right now — active enrollments.
``at_position``
    Where those people are standing. SPEC §12's editor shows it per step, and it
    is one grouped query for the whole sequence rather than one per rung.
"""

from typing import Any

from django.db.models import Count, Q, QuerySet

from apps.campaigns.models import EnrollmentStatus, Sequence, SequenceEnrollment, SequenceStep

__all__ = ["at_position", "sequences_for", "steps_for", "subscribers_for"]

#: Subscriber pages beyond this are a report, not a page. The list is ordered by
#: recency so the cap keeps the useful end.
MAX_SUBSCRIBERS = 200


def sequences_for(workspace: Any, *, query: str = "", status: str = "") -> QuerySet[Sequence]:
    """The workspace's sequences, filtered by the toolbar, with two counts.

    Both counts are conditional aggregates over one join rather than two
    subqueries, so the list page costs one statement whatever it contains.
    """
    rows = Sequence.objects.for_workspace(workspace)
    if query:
        rows = rows.filter(name__icontains=query)
    if status:
        rows = rows.filter(status=status)
    return rows.annotate(
        subscriber_count=Count("enrollments", filter=Q(enrollments__status=EnrollmentStatus.ACTIVE), distinct=True),
        step_count=Count("steps", distinct=True),
    ).order_by("name")


def steps_for(sequence: Sequence) -> list[SequenceStep]:
    """This sequence's steps in order, each carrying how many people are on it.

    The count is attached in Python from one grouped query. Annotating it onto
    the step rows would need a join from step to enrollment on ``position ==
    current_step``, which is a condition no foreign key expresses — enrollments
    track a position, deliberately, so that deleting a step cannot cascade an
    enrollment away.
    """
    counts = at_position(sequence)
    steps = list(
        SequenceStep.objects.for_workspace(sequence.workspace_id)
        .filter(sequence=sequence)
        .select_related("flow")
        .order_by("position")
    )
    for step in steps:
        step.waiting_count = counts.get(step.position, 0)  # type: ignore[attr-defined]
    return steps


def at_position(sequence: Sequence) -> dict[int, int]:
    """``{position: how many active enrollments are waiting on it}``. One query."""
    rows = (
        SequenceEnrollment.objects.for_workspace(sequence.workspace_id)
        .filter(sequence=sequence, status=EnrollmentStatus.ACTIVE)
        .values("current_step")
        .annotate(total=Count("id"))
    )
    return {int(row["current_step"]): int(row["total"]) for row in rows}


def subscribers_for(sequence: Sequence, *, status: str = EnrollmentStatus.ACTIVE) -> list[SequenceEnrollment]:
    """The enrollment rows for the subscriber panel, newest first.

    ``select_related("contact")`` because every row renders a name; without it
    this is the page's N+1.
    """
    rows = SequenceEnrollment.objects.for_workspace(sequence.workspace_id).filter(sequence=sequence)
    if status:
        rows = rows.filter(status=status)
    return list(rows.select_related("contact").order_by("-created_at")[:MAX_SUBSCRIBERS])
