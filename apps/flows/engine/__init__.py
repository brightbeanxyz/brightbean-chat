"""The flow engine — SPEC §9, issue #9 (L3-B).

The public surface of the runtime, and the only names another layer should
import. Everything under this package is free to move; these are not.

    from apps.flows.engine import start_flow, resume_execution, synchronous_safe

**What lives where.** ``runner`` owns the loop, the lock and every write to a
``FlowExecution``; ``registry`` is ROADMAP contract 5, the two additive
registries later layers extend; ``nodes/`` holds one class per node type;
``results`` is SPEC §9.2's five-way vocabulary; ``graph`` and ``context`` are
plumbing. The SSTI-safe renderer is deliberately *outside* this package, at
``apps/flows/rendering.py``, because SECURITY-BASELINE §3 names that path and
because nodes are not its only consumer.

**Registration happens in ``FlowsConfig.ready()``**, not here: importing
``apps.flows.engine`` must stay cheap enough for a module that only wants
``synchronous_safe``, and node classes import models.
"""

from apps.flows.engine.context import NodeContext
from apps.flows.engine.graph import Graph
from apps.flows.engine.registry import (
    DuplicateNodeTypeError,
    DuplicateVerbError,
    UnknownNodeTypeError,
    node_class_for,
    register_node,
    register_verb,
    registered_node_types,
    registered_verbs,
    synchronous_safe,
    types_without_runtime,
    unregister_node,
    unregister_verb,
    verb_handler,
)
from apps.flows.engine.results import Continue, End, Fail, Schedule, StartNext, StepResult, Wait
from apps.flows.engine.runner import (
    LOOP_CAP,
    EngineError,
    FlowNotRunnableError,
    advance,
    resume_execution,
    start_flow,
    stop_automation,
)
from apps.flows.engine.waits import Consumed, NotConsumed, ResumeOutcome, attempt_resume

__all__ = [
    "LOOP_CAP",
    "Consumed",
    "Continue",
    "DuplicateNodeTypeError",
    "DuplicateVerbError",
    "End",
    "EngineError",
    "Fail",
    "FlowNotRunnableError",
    "Graph",
    "NodeContext",
    "NotConsumed",
    "Schedule",
    "StartNext",
    "ResumeOutcome",
    "StepResult",
    "UnknownNodeTypeError",
    "Wait",
    "advance",
    "attempt_resume",
    "node_class_for",
    "register_node",
    "register_verb",
    "registered_node_types",
    "registered_verbs",
    "resume_execution",
    "start_flow",
    "stop_automation",
    "synchronous_safe",
    "types_without_runtime",
    "unregister_node",
    "unregister_verb",
    "verb_handler",
]
