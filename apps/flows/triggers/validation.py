"""Validate a trigger's ``config_json`` — caps first, then the schema.

The order is copied from :func:`apps.flows.schema.envelope.check_limits`, and it
is the order for a reason: measuring depth by walking a document you have not
bounded is how a deeply nested payload becomes a recursion error instead of a
validation finding. So size, then depth, then the schema walk — and each of the
first two returns immediately, because a document that failed them cannot be
meaningfully described by the findings the third would produce.

Never raises. A finding refuses the write and is rendered in the panel; an
exception would be a 500 on a form somebody typed into.
"""

import json
from typing import Any

from apps.flows.schema.envelope import json_depth
from apps.flows.schema.issues import Issue
from apps.flows.schema.jsonschema import CODE_INVALID_VALUE, validate_instance
from apps.flows.schema.nodes import all_defs
from apps.flows.triggers.registry import spec_for

__all__ = [
    "MAX_TRIGGER_CONFIG_BYTES",
    "MAX_TRIGGER_CONFIG_DEPTH",
    "config_byte_size",
    "validate_config",
]

#: A trigger config is a keyword list, not a graph. Three orders of magnitude
#: below ``MAX_GRAPH_BYTES`` because there is nothing here that legitimately
#: grows: the widest shape is ``MAX_KEYWORDS`` keywords of ``MAX_KEYWORD_CHARS``.
MAX_TRIGGER_CONFIG_BYTES = 16 * 1024

#: The deepest legitimate path is ``config → public_reply → texts → item`` at
#: four. Eight leaves room for the rule trigger's nested condition filter.
MAX_TRIGGER_CONFIG_DEPTH = 8


def config_byte_size(config: Any) -> int:
    """Serialized size, or a number over the cap when it will not serialize.

    A config that cannot be JSON-encoded cannot be stored in a jsonb column
    either, so answering "too big" is the honest outcome — it routes to the same
    refusal rather than to a traceback.
    """
    try:
        return len(json.dumps(config, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError):
        return MAX_TRIGGER_CONFIG_BYTES + 1


def validate_config(trigger_type: str, config: Any, *, known_size: int | None = None) -> list[Issue]:
    """Findings for ``config`` against ``trigger_type``'s schema. Empty means valid.

    ``known_size`` lets a caller that already measured the request body skip
    re-serializing, the same shortcut ``check_limits`` offers.
    """
    spec = spec_for(trigger_type)
    if spec is None:
        return [_issue(f"{trigger_type!r} is not a trigger type.", path="type")]

    if not isinstance(config, dict):
        return [_issue("A trigger's configuration has to be an object.", path="config")]

    size = known_size if known_size is not None else config_byte_size(config)
    if size > MAX_TRIGGER_CONFIG_BYTES:
        return [_issue(f"The configuration is {size} bytes; the limit is {MAX_TRIGGER_CONFIG_BYTES}.", path="config")]

    if json_depth(config, limit=MAX_TRIGGER_CONFIG_DEPTH) > MAX_TRIGGER_CONFIG_DEPTH:
        return [_issue(f"The configuration nests deeper than {MAX_TRIGGER_CONFIG_DEPTH} levels.", path="config")]

    return validate_instance(spec.config, config, path="config", defs=all_defs())


def _issue(message: str, *, path: str) -> Issue:
    return Issue(code=CODE_INVALID_VALUE, message=message, stage="document", node_id=None, path=path)
