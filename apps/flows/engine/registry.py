"""ROADMAP contract 5: the node-class and action-verb runtime registries.

    New flow-node types register ``type -> NodeClass``; action-node verbs
    register in an **action-verb registry** (L6-A adds ``subscribe_sequence`` /
    ``unsubscribe_sequence``); both additive, with config schemas appended to
    the L2-D module.

Two registries here, and it is worth being precise about how they relate to the
two in :mod:`apps.flows.schema.nodes`, because the names rhyme:

===========================  =============================  ====================
Question                     Registry                       Owner
===========================  =============================  ====================
What may a node's config     ``schema.nodes.NODE_TYPES``    L2-D (#6)
contain, and what handles    ``register_node_type(spec)``
does it expose?
What *runs* when the engine  ``engine.registry`` (here)     L3-B (#9)
reaches that node?           ``register_node(cls)``
Which verbs may an action    ``schema.nodes.ACTION_VERBS``  L2-D (#6)
node's config name?          ``register_action_verb(...)``
What does a verb *do*?       ``engine.registry`` (here)     L3-B (#9)
                             ``register_verb(verb, fn)``
===========================  =============================  ====================

The split is what lets a node type ship its schema in Layer 2 and its runtime in
Layer 4 or 5 — ``send_sms`` and ``send_email`` still have validated, drawable
schemas and no runtime, and ``external_request`` was in that state until L4-E —
and it is what lets L6-A add sequence *behaviour* without touching a schema that
already describes it.

**Registration is additive and loud.** Registering a second class for a type
raises rather than replacing: two apps quietly claiming one node type surfaces
as a flow running under the wrong code, which is far worse than an import-time
crash. ``replace=True`` exists for tests and for a deliberate override.

**A runtime with no schema is refused outright.** ``register_node`` checks
``node_spec(cls.type)`` and raises when nothing describes the type. Without that
check a typo'd ``type`` would register a node the validator has never heard of,
the builder cannot draw, and no graph can ever legally contain — dead code that
looks wired up.
"""

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from apps.flows.schema import ACTION_VERBS, NODE_TYPES, node_spec

if TYPE_CHECKING:
    from apps.flows.engine.context import NodeContext
    from apps.flows.engine.nodes.base import Node

__all__ = [
    "DuplicateNodeTypeError",
    "DuplicateVerbError",
    "UnknownNodeTypeError",
    "VerbHandler",
    "node_class_for",
    "register_node",
    "register_verb",
    "registered_node_types",
    "registered_verbs",
    "synchronous_safe",
    "types_without_runtime",
    "unregister_node",
    "unregister_verb",
    "verb_handler",
]

logger = logging.getLogger(__name__)

#: ``handler(ctx, step) -> None``. ``step`` is the one entry from the action
#: node's ``actions`` list, config schema already validated at publish.
VerbHandler = Callable[["NodeContext", dict[str, Any]], None]

_NODES: dict[str, type["Node"]] = {}
_VERBS: dict[str, VerbHandler] = {}


class DuplicateNodeTypeError(RuntimeError):
    """Two node classes registered for one node type."""


class DuplicateVerbError(RuntimeError):
    """Two handlers registered for one action verb."""


class UnknownNodeTypeError(LookupError):
    """A graph names a node type nothing has registered a runtime for."""


def register_node(node_class: type["Node"], *, replace: bool = False) -> type["Node"]:
    """Register a node class. Usable bare or as a decorator."""
    node_type = getattr(node_class, "type", "")
    if not node_type:
        raise ValueError(f"{node_class.__name__} has no `type`; a node class is registered by its graph type.")
    if node_spec(node_type) is None:
        raise ValueError(
            f"Node type {node_type!r} has no NodeSpec in apps.flows.schema.nodes, so no graph can "
            f"legally contain it. Register the config schema there first (ROADMAP contract 2); "
            f"known types: {', '.join(sorted(NODE_TYPES))}."
        )
    existing = _NODES.get(node_type)
    if existing is not None and not replace and existing is not node_class:
        raise DuplicateNodeTypeError(
            f"Node type {node_type!r} is already run by {existing.__module__}.{existing.__qualname__}. "
            f"Node types are a shared namespace across every app; pass replace=True if the override "
            f"is deliberate."
        )
    _NODES[node_type] = node_class
    logger.debug("Registered node runtime %s -> %s.%s", node_type, node_class.__module__, node_class.__qualname__)
    return node_class


def unregister_node(node_type: str) -> None:
    """Remove a node runtime. Unknown types are ignored.

    The counterpart to :func:`register_node`, and the same shape as
    :func:`apps.channels.ingest.unregister_processor`. It exists because
    ``replace=True`` cannot undo the case that matters: a test installing a stub
    for a type that has **no** runtime in this layer — ``send_message`` before
    #9's PR 2, ``send_sms`` and ``send_email`` until L5-D/E — has nothing to put
    back, and
    without a removal the stub outlives the test and the next module sees a node
    type this deployment does not actually implement.
    """
    _NODES.pop(node_type, None)


def node_class_for(node_type: str) -> type["Node"] | None:
    """The class that runs this node type, or ``None`` when nothing does."""
    return _NODES.get(node_type)


def registered_node_types() -> tuple[str, ...]:
    """Every node type with a runtime, sorted."""
    return tuple(sorted(_NODES))


def types_without_runtime() -> tuple[str, ...]:
    """Node types the schema describes but nothing can execute yet.

    Not a fault — it is the normal state of a layered build, and the set is
    pinned by a test so that a type joining or leaving it is a deliberate act
    rather than something noticed in production.
    """
    return tuple(sorted(set(NODE_TYPES) - set(_NODES)))


def synchronous_safe(node_type: str) -> bool:
    """May this node run inline in the webhook request (SPEC §7.1)?

    Read by L4-A's inline-vs-enqueue budget. SPEC names the safe set explicitly
    — "send message, action, condition, randomizer, start flow" — and each node
    class carries the answer as a class attribute rather than this function
    holding a second list that could disagree with it.

    An unregistered type answers ``False``: something that cannot be run at all
    certainly cannot be run inside a 1.5-second budget.
    """
    node_class = _NODES.get(node_type)
    return bool(node_class is not None and node_class.synchronous_safe)


def register_verb(verb: str, handler: VerbHandler, *, replace: bool = False) -> VerbHandler:
    """Register an action-node verb's runtime (SPEC §11.2).

    The verb must already exist in the schema registry: a verb with a runtime
    and no schema can never appear in a published graph, because validation
    rejects the config that would name it.
    """
    if verb not in ACTION_VERBS:
        raise ValueError(
            f"Action verb {verb!r} has no schema in apps.flows.schema.nodes, so no graph can name it. "
            f"Register it with register_action_verb() first (ROADMAP contract 2/5); "
            f"known verbs: {', '.join(sorted(ACTION_VERBS))}."
        )
    existing = _VERBS.get(verb)
    if existing is not None and not replace and existing is not handler:
        raise DuplicateVerbError(
            f"Action verb {verb!r} is already handled by "
            f"{existing.__module__}.{getattr(existing, '__qualname__', existing)}."
        )
    _VERBS[verb] = handler
    return handler


def verb_handler(verb: str) -> VerbHandler | None:
    """The runtime for this verb, or ``None`` while its owner has not landed.

    ``None`` is the live answer for ``subscribe_sequence`` and
    ``unsubscribe_sequence`` until L6-A (#22) registers them. The action node
    logs and moves on rather than failing, so a flow that also adds a tag still
    adds the tag.
    """
    return _VERBS.get(verb)


def unregister_verb(verb: str) -> None:
    """Remove a verb runtime. Unknown verbs are ignored. See :func:`unregister_node`."""
    _VERBS.pop(verb, None)


def registered_verbs() -> tuple[str, ...]:
    """Every verb with a runtime, sorted."""
    return tuple(sorted(_VERBS))
