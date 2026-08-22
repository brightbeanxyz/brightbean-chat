"""What a node hands back — SPEC §9.2's ``StepResult``, all five of it.

    Node classes implement ``execute(ctx) -> StepResult`` where StepResult is
    one of: ``Continue(next_handle)``, ``Wait(wait_config)``,
    ``Schedule(run_at, resume_handle)``, ``End``, ``Fail(error)``.

Five, and the set is closed on purpose. A node describes *what happened*; the
runner alone decides what that means for the row in the database — which status
to write, whether the loop-cap counter moves, whether a queue action is needed.
Every time a node has been tempted to write ``execution.status`` itself, the
answer has been to add meaning to one of these five rather than a sixth path
through the state machine.

The one place that shape strains is SPEC §11.3's ``start_flow`` node, which
"terminates current execution (completed), starts target flow's published
version immediately under the same lock". That is an ``End`` followed by a
start, so :class:`End` carries an optional :class:`StartNext` rather than
becoming a sixth result. The node still only reports; the runner still owns
every write, and it performs the hand-off *after* the terminal write so the
completed execution and its ``execution.completed`` event are durable before
the next flow begins.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

__all__ = ["Continue", "End", "Fail", "Schedule", "StartNext", "StepResult", "Wait"]


@dataclass(frozen=True)
class Continue:
    """Follow ``handle`` out of this node and keep running.

    A handle with no edge is not an error — SPEC §9.2: "Missing edge for a
    handle -> End". A flow that simply stops after its last message is the
    common case, not a broken graph.
    """

    handle: str = "default"


@dataclass(frozen=True)
class Wait:
    """Pause until an inbound event matches ``config`` (SPEC §9.3).

    ``config`` is the JSON that goes into ``flow_execution.wait_config``; its
    shapes and the token discipline live in :mod:`apps.flows.engine.waits`.
    Resets the loop-cap counter, because waiting is the pause the cap counts
    blocks since.
    """

    config: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Schedule:
    """Pause until ``run_at``, then resume through ``resume_handle``.

    Distinct from :class:`Wait` because nothing a contact does can shorten it:
    a smart delay is resumed only by its own ``scheduled_action`` (SPEC §9.3),
    and the status the runner writes says so (``waiting_delay``).
    """

    run_at: datetime
    resume_handle: str = "default"
    #: Extra state the resuming handler needs, merged into ``wait_config``.
    config: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StartNext:
    """The hand-off SPEC §11.3 describes: end here, begin ``flow_id`` there."""

    flow_id: str


@dataclass(frozen=True)
class End:
    """This execution is finished, successfully.

    ``start_next`` is set only by the ``start_flow`` node; see the module
    docstring for why it lives here rather than in a sixth result type.
    """

    start_next: StartNext | None = None


@dataclass(frozen=True)
class Fail:
    """This execution cannot continue. ``error`` is stored and shown to admins.

    Reserved for the run being broken — a loop with no pause in it, a node type
    with no runtime, a condition whose filter no longer validates. **Not** for a
    message that failed to send: SPEC §9.5 is explicit that a provider error
    fails the message and follows the ``default`` edge onward, so a send failure
    is a :class:`Continue`.
    """

    error: str


#: Anything ``Node.execute`` may return.
type StepResult = Continue | Wait | Schedule | End | Fail
