"""The ordered inbound-hook registry — ROADMAP contract 6, second half.

    L4-A replaces the tail with an **ordered hook registry with named stages**:
    ``hard_optout → post_persist → resume → trigger → default_reply``. Later
    streams register hooks instead of editing routing code: L5-D registers
    STOP/HELP at ``hard_optout``, L6-C registers inbox rules at ``post_persist``.

**This is the deliverable, not an internal detail**, so two properties are worth
more than convenience.

*It imports nothing from this project.* L5-D and L6-C register from their own
``AppConfig.ready()``, and an import that dragged in models, the engine or
``apps.messaging`` would make the order in which apps are readied matter. The
context object is a ``TYPE_CHECKING`` import only.

*Order does not depend on registration order.* Hooks in a stage are dispatched
by ``(priority, name)``, sorted when they are read. ``INSTALLED_APPS`` order is
not a contract anybody should have to know, and "stage registration order is
deterministic" is an acceptance criterion of this issue rather than a hope.

Two error rules, because "a label failed to apply" and "STOP was ignored" are
not the same accident. Everywhere except ``hard_optout``, a raising hook is
logged and treated as though it had passed — one broken inbox rule must not cost
the reply. At ``hard_optout`` it aborts the event instead: if we cannot establish
that this contact has *not* opted out, nothing downstream may send them anything.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only, see the module docstring
    from apps.flows.triggers.context import RoutingContext

__all__ = [
    "ENGINE_STAGES",
    "RUNS_WHILE_PAUSED",
    "STAGE_ORDER",
    "Consumed",
    "Deferred",
    "DuplicateHookError",
    "HookAbortedError",
    "HookOutcome",
    "HookRegistration",
    "Passed",
    "Stage",
    "UnknownStageError",
    "hook_names",
    "register_hook",
    "registered_hooks",
    "run_stage",
    "stages_from",
    "unregister_hook",
]

logger = logging.getLogger(__name__)


class Stage(StrEnum):
    """Contract 6's five named stages."""

    HARD_OPTOUT = "hard_optout"
    POST_PERSIST = "post_persist"
    RESUME = "resume"
    TRIGGER = "trigger"
    DEFAULT_REPLY = "default_reply"


#: Dispatch order. A tuple rather than dict-insertion order: contract 6 fixes
#: this sequence, and it must not depend on which app imported what first.
STAGE_ORDER: tuple[Stage, ...] = (
    Stage.HARD_OPTOUT,
    Stage.POST_PERSIST,
    Stage.RESUME,
    Stage.TRIGGER,
    Stage.DEFAULT_REPLY,
)

#: Stages that still run while ``conversation.automation_paused_until`` is in the
#: future (SPEC §9.3, §14: an agent has taken over "except opt-out handling").
#:
#: Structural rather than a check inside each hook, so a hook a later layer
#: registers at ``trigger`` inherits the rule instead of remembering it.
#: ``post_persist`` is in here because inbox rules are inbox features — labels,
#: assignment — and suppressing those during a takeover would break the takeover.
RUNS_WHILE_PAUSED: frozenset[Stage] = frozenset({Stage.HARD_OPTOUT, Stage.POST_PERSIST})

#: Stages that advance a contact's state machine, and therefore need the
#: advisory lock, a transaction, and the SPEC §7.1 inline budget.
ENGINE_STAGES: frozenset[Stage] = frozenset({Stage.RESUME, Stage.TRIGGER, Stage.DEFAULT_REPLY})


@dataclass(frozen=True)
class Passed:
    """This hook has nothing to say about the event. Keep walking."""

    reason: str = ""


@dataclass(frozen=True)
class Consumed:
    """The event is spoken for. Stop — no later stage runs.

    ``by`` is stamped by :func:`run_stage`, never by the hook: a hook naming
    someone else as the consumer would make the log a work of fiction.
    """

    reason: str = ""
    by: str = ""


@dataclass(frozen=True)
class Deferred:
    """This cannot be finished in the request. Hand the event to the worker.

    Distinct from ``Consumed`` because the event is *not* dealt with, and
    distinct from ``Passed`` because later stages must not run inline either —
    the queue will replay them from here.
    """

    reason: str = ""


#: ``None`` is accepted and means :class:`Passed`, so a hook that only *does*
#: something — L6-C applying a label — can simply return.
HookOutcome = Passed | Consumed | Deferred | None

Hook = Callable[["RoutingContext"], HookOutcome]


class DuplicateHookError(RuntimeError):
    """Two hooks registered under one name."""


class UnknownStageError(LookupError):
    """A hook named a stage that is not in :data:`STAGE_ORDER`."""


class HookAbortedError(RuntimeError):
    """A ``hard_optout`` hook failed, so the event is abandoned. Fail closed."""


@dataclass(frozen=True)
class HookRegistration:
    """One registered hook."""

    name: str
    stage: Stage
    priority: int
    hook: Hook


_HOOKS: dict[str, HookRegistration] = {}


def register_hook(
    hook: Hook,
    *,
    stage: Stage | str,
    name: str,
    priority: int = 100,
    replace_existing: bool = False,
) -> Hook:
    """Register ``hook`` to run at ``stage``.

    ``name`` is unique **across every stage**, not within one: a name identifies
    a behaviour ("stop_keywords", "inbox_rules"), which means
    :func:`unregister_hook` needs only the name, and the same behaviour cannot be
    quietly registered at two stages at once.

    A duplicate raises rather than replacing, unlike
    ``apps.channels.ingest.register_processor``. That seam replaces because a
    later layer taking over a stage its predecessor stubbed is the intended
    lifecycle *there*; here, two apps claiming one behaviour is a bug.

    ``priority`` is dispatch order within the stage, lower first — the same
    convention triggers use. Built-ins sit at 100 so a later stream can go
    before or after them without renumbering anything.
    """
    resolved = _stage(stage)
    if not name:
        raise ValueError("A routing hook needs a name so it can be replaced or removed later.")
    existing = _HOOKS.get(name)
    if existing is not None and not replace_existing:
        raise DuplicateHookError(
            f"{name!r} is already registered at stage {existing.stage}. "
            f"Pick a distinct name, or pass replace_existing=True if the override is deliberate."
        )
    _HOOKS[name] = HookRegistration(name=name, stage=resolved, priority=priority, hook=hook)
    logger.debug("Registered routing hook %r at %s (priority %s)", name, resolved, priority)
    return hook


def unregister_hook(name: str) -> None:
    """Remove a hook. Unknown names are ignored."""
    _HOOKS.pop(name, None)


def registered_hooks(stage: Stage | str | None = None) -> tuple[HookRegistration, ...]:
    """Registrations in dispatch order — every stage, or just one.

    Built fresh from a sort each call, so a hook registered while a stage is
    being dispatched cannot mutate the sequence already being walked.
    """
    if stage is None:
        return tuple(
            sorted(_HOOKS.values(), key=lambda item: (STAGE_ORDER.index(item.stage), item.priority, item.name))
        )
    resolved = _stage(stage)
    return tuple(
        sorted(
            (item for item in _HOOKS.values() if item.stage is resolved),
            key=lambda item: (item.priority, item.name),
        )
    )


def hook_names() -> tuple[str, ...]:
    """Every hook name, flattened into dispatch order. For tests and for ops."""
    return tuple(item.name for item in registered_hooks())


def stages_from(stage: Stage | str) -> tuple[Stage, ...]:
    """``STAGE_ORDER`` from ``stage`` onwards — how a deferred run resumes."""
    resolved = _stage(stage)
    return STAGE_ORDER[STAGE_ORDER.index(resolved) :]


def run_stage(stage: Stage | str, context: "RoutingContext") -> Passed | Consumed | Deferred:
    """Walk one stage's hooks, stopping at the first that consumes or defers."""
    resolved = _stage(stage)
    for registration in registered_hooks(resolved):
        outcome = _run_one(registration, context)
        if isinstance(outcome, Consumed):
            return replace(outcome, by=registration.name)
        if isinstance(outcome, Deferred):
            return outcome
    return Passed()


def _run_one(registration: HookRegistration, context: "RoutingContext") -> Passed | Consumed | Deferred:
    from contextlib import nullcontext

    from django.db import connection as db
    from django.db import transaction

    # A savepoint per hook, but only when there is something to protect.
    # Catching a hook's IntegrityError *inside* a transaction without a
    # savepoint poisons that transaction — and on the engine stages the
    # transaction being poisoned is the one holding the contact advisory lock.
    # Outside one there is nothing to roll back to, each of the hook's writes
    # has already committed on its own, and opening a transaction per hook would
    # cost a round trip to protect nothing. Reading `in_atomic_block` is an
    # attribute lookup, not a query, so this stays usable without a database.
    guard = transaction.atomic() if db.in_atomic_block else nullcontext()

    try:
        with guard:
            outcome = registration.hook(context)
    except Exception:
        logger.exception("Routing hook %r failed at stage %s", registration.name, registration.stage)
        if registration.stage is Stage.HARD_OPTOUT:
            raise HookAbortedError(registration.name) from None
        return Passed(f"{registration.name} failed")
    return outcome if outcome is not None else Passed()


def _stage(stage: Stage | str) -> Stage:
    try:
        return Stage(stage)
    except ValueError:
        raise UnknownStageError(
            f"{stage!r} is not a routing stage. Contract 6's stages are: {', '.join(STAGE_ORDER)}."
        ) from None
